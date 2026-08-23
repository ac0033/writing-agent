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
    links = re.findall(r"canonical_url:\s*(\S+)", fm)
    ev = re.search(r"evidence_sources:\s*\n((?:\s+-\s+\S+\n?)+)", fm)
    if ev:
        links.extend(re.findall(r"-\s*(\S+)", ev.group(1)))
    return list(dict.fromkeys(links))  # 保序去重


def _strip_frontmatter(text: str) -> tuple[str, str]:
    """拆出 YAML frontmatter，返回 (frontmatter, 正文)。"""
    m = re.match(r"^---\n(.*?)\n---\n?", text, re.S)
    if not m:
        return "", text
    return m.group(1), text[m.end():]


def _get_index(name: str) -> dict:
    if name in _indexes:
        return _indexes[name]
    from rank_bm25 import BM25Okapi
    if name == "corpus":
        directory, suffixes, label = config.CORPUS_DIR, (".md", ".txt", ".pdf"), "素材库"
    else:
        directory, suffixes, label = config.WIKI_DIR, (".md",), "知识库"

    chunks: list[dict] = []
    if directory.is_dir():
        for path in sorted(directory.rglob("*")):
            if path.is_file() and path.suffix.lower() in suffixes:
                try:
                    text = _extract(path)
                    links = []
                    if name == "wiki":
                        fm, text = _strip_frontmatter(text)
                        links = _wiki_links(fm)
                    for c in _chunk(text):
                        chunks.append({"source": path.name, "text": c, "links": links})
                except Exception as e:
                    print(f"[{label}] ⚠️ 解析失败：{path.name}：{e}")
    tokens = [_bigrams(c["text"]) for c in chunks]
    idx = {"chunks": chunks, "bm25": BM25Okapi(tokens) if tokens else None, "tokens": tokens}
    _indexes[name] = idx
    print(f"[{label}] 索引完成：{len(chunks)} 块，来自 {directory}")
    return idx


def _search(name: str, query: str, top_k: int, empty_msg: str) -> str:
    idx = _get_index(name)
    if not idx["bm25"]:
        return empty_msg
    scores = idx["bm25"].get_scores(_bigrams(query))
    top = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:top_k]
    if not top or scores[top[0]] <= 0:
        return "没有检索到相关内容。"
    blocks = []
    for i in top:
        c = idx["chunks"][i]
        block = f"【出自：{c['source']}】（相关度 {scores[i]:.1f}）\n{c['text']}"
        if c["links"]:
            block += "\n可引用来源：" + "、".join(c["links"])
        blocks.append(block)
    return "\n\n---\n\n".join(blocks)


def search_corpus(query: str, top_k: int = 3) -> str:
    """agent5 的风格素材检索。"""
    return _search("corpus", query, top_k, "素材库为空或没有可检索的内容。")


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
    "检索写作素材库（风格范例、逻辑框架、可引用的观点）。query 用关键词短语，中英文均可；返回最相关的若干片段及出处文件名。",
)
WIKI_TOOL_SCHEMA = _schema(
    "search_wiki",
    "检索 llm_wiki 知识库（AI Agent 主题的文献笔记，含概念解释、事实依据和一手来源链接）。query 用关键词短语，中英文均可；返回相关片段、出处页面和可引用来源 URL。",
)
