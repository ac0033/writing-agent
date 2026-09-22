"""本地检索：BM25，无外部 API，每次运行全量重建索引。

目前有两个检索源：
- corpus（config.CORPUS_DIR）：写作素材库，agent5 的风格范本，支持 .md/.txt/.pdf；
- wiki（config.WIKI_DIR）：llm_wiki 知识库的知识层，agent1/agent2 的事实与概念依据，
  只索引 .md 页面，并从 frontmatter 提取 canonical_url / evidence_sources 作为可引用链接。

两个源相互独立、各自建索引；索引在首次检索时构建，进程内缓存，
每次启动新进程都会重新扫描整个目录——目录内容增删后下次运行自动完整接入。

为什么用 BM25 而不是 embedding：查询和命中都以关键词/术语为主，BM25 够用；
且零 API 成本、零网络依赖。中文用词符二元组（bigram）切分，中英混排都能检索。
"""
import re
from pathlib import Path

import config
from log import log

CHUNK_SIZE = 500      # 每块约 500 字
CHUNK_OVERLAP = 80    # 块间重叠，避免切断上下文

_indexes: dict[str, dict] = {}  # name -> {chunks, bm25, tokens}


def _bigrams(text: str) -> list[str]:
    """词符二元组切分：对中文等效于滑窗，对英文单词退化为单词本身。"""
    text = re.sub(r"\s+", " ", text.lower())
    tokens = []
    for word in text.split(" "):
        if len(word) <= 2:
            tokens.append(word)
        else:
            tokens.extend(word[i:i + 2] for i in range(len(word) - 1))
    return tokens


def _extract(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in (".md", ".txt"):
        return path.read_text(encoding="utf-8", errors="replace")
    if suffix == ".pdf":
        from pypdf import PdfReader
        reader = PdfReader(str(path))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    return ""


def _chunk(text: str) -> list[str]:
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    chunks, start = [], 0
    while start < len(text):
        chunks.append(text[start:start + CHUNK_SIZE])
        start += CHUNK_SIZE - CHUNK_OVERLAP
    return [c for c in chunks if len(c.strip()) > 50]


def _wiki_links(fm: str) -> list[str]:
    """从 wiki 页面 frontmatter 提取可引用的来源 URL（canonical_url / evidence_sources），去重。"""
    from tools.evidence import canonical_url
    fields = re.findall(r"(?m)^(?:canonical_url|evidence_sources):[^\n]*(?:\n[ \t]+[^\n]*)*", fm)
    return list(dict.fromkeys(u for field in fields
                             for raw in re.findall(r"https?://[^\s\]\"',]+", field)
                             if (u := canonical_url(raw))))



def _strip_frontmatter(text: str) -> tuple[str, str]:
    """拆出 YAML frontmatter，返回 (frontmatter, 正文)。"""
    m = re.match(r"^---\n(.*?)\n---\n?", text, re.S)
    if not m:
        return "", text
    return m.group(1), text[m.end():]


def _get_index(name: str) -> dict:
    from rank_bm25 import BM25Okapi
    if name == "corpus":
        directory, suffixes, label = config.CORPUS_DIR, (".md", ".txt", ".pdf"), "素材库"
    elif name == "author":
        directory = Path(getattr(config, "AUTHOR_STYLE_DIR", config.CORPUS_DIR / "_author_unconfigured"))
        suffixes, label = (".md", ".txt"), "作者旧文"
    else:
        directory, suffixes, label = config.WIKI_DIR, (".md",), "知识库"

    directory = Path(directory)
    paths = sorted(p for p in directory.rglob("*") if p.is_file() and p.suffix.lower() in suffixes
                   and p.resolve().is_relative_to(directory.resolve())
                   and p.name not in ("index.md", "log.md")) if directory.is_dir() else []
    signature = tuple((str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in paths)
    if name in _indexes and _indexes[name].get("signature") == signature:
        return _indexes[name]
    chunks: list[dict] = []
    if directory.is_dir():
        for path in paths:
            if path.is_file() and path.suffix.lower() in suffixes:
                try:
                    text = _extract(path)
                    links = []
                    status, verified = "", ""
                    if name == "wiki":
                        fm, text = _strip_frontmatter(text)
                        links = _wiki_links(fm)
                        def field(key):
                            match = re.search(r"(?m)^" + key + r":\s*([^\n]+)", fm)
                            return match.group(1).strip().strip("\"'") if match else "unknown"
                        status, verified = field("status"), field("last_verified")
                        if status == "archived":
                            continue
                    for c in _chunk(text):
                        chunks.append({"source": path.relative_to(directory).as_posix(), "text": c, "links": links, "status": status, "last_verified": verified})
                except Exception as e:
                    log(f"[{label}] ⚠️ 解析失败：{path.name}：{e}")
    tokens = [_bigrams(c["text"]) for c in chunks]
    idx = {"chunks": chunks, "bm25": BM25Okapi(tokens) if tokens else None, "tokens": tokens, "signature": signature}
    _indexes[name] = idx
    log(f"[{label}] 索引完成：{len(chunks)} 块，来自 {directory}")
    return idx


def _search(name: str, query: str, top_k: int, empty_msg: str) -> str:
    top_k = max(1, min(8, int(top_k)))
    idx = _get_index(name)
    if not idx["bm25"]:
        return empty_msg
    scores = idx["bm25"].get_scores(_bigrams(query))
    # 只有一两篇作者旧文时 BM25 的 IDF 可能为零；仍保留确实命中的词符。
    if name in ("author", "corpus") and not any(score > 0 for score in scores):
        query_tokens = set(_bigrams(query)) - {""}
        scores = [len(query_tokens.intersection(tokens)) for tokens in idx["tokens"]]
    top = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:top_k]
    if not top or scores[top[0]] <= 0:
        return "没有检索到相关内容。"
    blocks = []
    for i in top:
        if scores[i] <= 0:
            continue
        c = idx["chunks"][i]
        source = "author/" + c["source"] if name == "author" else c["source"]
        block = f"【出自：{source}】（相关度 {scores[i]:.1f}）\n{c['text']}"
        if name in ("corpus", "author"):
            block += f"\n用途：写法参考，不作为事实证据。可用 read_corpus(source={source!r}) 读取连续正文。"
        if name == "wiki":
            block += f"\n笔记状态：{c['status']}；最近核验：{c['last_verified']}。笔记是线索，引用前需读取对应原文。"
        if c["links"]:
            block += "\n待核对来源：" + "、".join(c["links"])
        blocks.append(block)
    return "\n\n---\n\n".join(blocks)


def search_corpus(query: str, top_k: int = 3) -> str:
    """检索作者旧文和素材库，仅用来参考写法。"""
    return "\n\n".join((
        _search("author", query, top_k, "作者旧文未配置或没有相关内容。"),
        _search("corpus", query, top_k, "素材库为空或没有可检索的内容。"),
    ))


def read_corpus(source: str, start_line: int = 1, max_lines: int = 160) -> str:
    """只读指定库内的连续正文，明确范围，拒绝越界路径和链接。"""
    source = source.replace("\\", "/")
    author = source.startswith("author/")
    root = Path(getattr(config, "AUTHOR_STYLE_DIR", config.CORPUS_DIR / "_author_unconfigured")) if author else Path(config.CORPUS_DIR)
    relative = source[7:] if author else source
    path = (root / relative).resolve()
    if not relative or Path(relative).is_absolute() or not path.is_relative_to(root.resolve()):
        return "读取失败：仅允许读取已配置素材库内的相对路径。"
    allowed = (".md", ".txt") if author else (".md", ".txt", ".pdf")
    if path.suffix.lower() not in allowed or not path.is_file():
        return "读取失败：文件不存在或不是支持的素材文件。"
    try:
        text = _extract(path)
    except Exception as exc:
        return f"读取失败：{type(exc).__name__}。"
    lines = text.splitlines()
    start = max(1, int(start_line))
    count = max(1, min(400, int(max_lines)))
    # 单行很长的 PDF 也不能绕过上下文预算；下一行位置保持可回查。
    selected, chars = [], 0
    partial = False
    for number, line in enumerate(lines[start - 1:start - 1 + count], start):
        if chars + len(line) > 24000:
            if not selected:
                selected.append(f"{number}: {line[:24000]} [本行截断，未读部分不可当作已读]")
                partial = True
            break
        selected.append(f"{number}: {line}")
        chars += len(line)
    end = start + len(selected) - 1
    truncated = partial or start > 1 or end < len(lines)
    next_line = start if partial else end + 1
    return (f"来源：{source}\n实际路径：{path}\n用途：写法参考，不作为事实证据，不移植经历。"
            f"\n行范围：{start}-{end} / {len(lines)}；未完整读取：{str(truncated).lower()}"
            f"；下一起始行：{next_line if end < len(lines) or partial else '无'}\n\n" + "\n".join(selected))


def search_wiki(query: str, top_k: int = 3) -> str:
    """agent1/agent2 的知识库检索：查概念解释、事实依据、可引用的来源链接。"""
    return _search("wiki", query, top_k, "知识库目录不存在或没有可检索的内容。")


def _schema(name: str, description: str) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "检索关键词短语"},
                    "top_k": {"type": "integer", "description": "返回片段数，默认 3", "default": 3},
                },
                "required": ["query"],
            },
        },
    }


# OpenAI function calling 格式的工具 schema
SEARCH_TOOL_SCHEMA = _schema(
    "search_corpus",
    "检索作者旧文与写作素材库，只参考文章展开和表达，不作为事实证据。返回片段和 source，可用 read_corpus 扩展成连续章节。",
)
READ_TOOL_SCHEMA = {
    "type": "function", "function": {
        "name": "read_corpus", "description": "只读检索命中的连续章节或文章，返回行范围和截断状态；作者旧文 source 以 author/ 开头。",
        "parameters": {"type": "object", "properties": {
            "source": {"type": "string", "description": "search_corpus 返回的库内相对路径"},
            "start_line": {"type": "integer", "default": 1},
            "max_lines": {"type": "integer", "default": 160},
        }, "required": ["source"]},
    },
}
WIKI_TOOL_SCHEMA = _schema(
    "search_wiki",
    "检索 llm_wiki 知识库（AI Agent 主题的文献笔记，含概念解释、事实依据和一手来源链接）。query 用关键词短语，中英文均可；返回相关片段、出处页面和可引用来源 URL。",
)


def wiki_candidates(query: str, top_k: int = 3) -> list[dict]:
    """研究员使用结构化笔记线索，最终证据仍须读取原文。"""
    idx = _get_index("wiki")
    if not idx["bm25"]:
        return []
    scores = idx["bm25"].get_scores(_bigrams(query))
    result = []
    for i in sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:top_k]:
        if scores[i] <= 0:
            continue
        c = idx["chunks"][i]
        for url in c["links"]:
            result.append({"url": url, "title": c["source"], "content": c["text"],
                           "wiki_status": c["status"], "query": query})
    return result
