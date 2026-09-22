"""记录证据与机械检查；不把链接存在或模型通过等同于事实证明。"""
import hashlib
import re
from collections import Counter
from datetime import date
from urllib.parse import urlsplit, urlunsplit

import config


def local_source_blocks(text: str) -> list[dict]:
    """按来源标记读取冻结输入内的连续摘录，不读取或扩大到磁盘全文。

    支持来源包的 来源/定位/保存副本 标记，以及独立一行的绝对路径。
    不认识的格式没有证据；多个来源的片段不能拼接后核对引文。
    """
    result, sources, body = [], [], []
    heading_level = None
    content_started = False
    # 节标题下、正文前声明的副本路径属于整节（如“保存副本：…（两个完整用户消息）”），
    # 该节里每个“来源”小块都能用它定位引文；遇到同级节标题才失效。
    section_sources, section_level = [], None
    last_heading_level = None

    def flush():
        nonlocal sources, body, content_started
        if sources and body:
            result.append({"source_paths": sources[:], "text": "\n".join(body)})
        sources, body, content_started = [], [], False

    for line in text.splitlines():
        value = line.strip()
        label = re.match(r"^(?:来源|定位|保存副本)[:：]\s*(.+)$", value)
        candidate = label.group(1) if label else value
        path = re.match(r"((?:[A-Za-z]:[\\/]|/).+?\.(?:md|txt|jsonl|json|csv|log|rst))(?=$|[:：，；])", candidate, re.I)
        if path and (label or candidate == path.group(1)):
            if content_started:
                flush()
            if not sources and section_sources:
                sources = section_sources[:]
            sources.append(path.group(1).replace("\\", "/"))
            if not content_started and label and label.group(0).startswith("保存副本") and last_heading_level is not None:
                section_sources, section_level = [path.group(1).replace("\\", "/")], last_heading_level
            continue
        heading = re.match(r"^(#{1,6})\s+", value)
        if heading:
            level = len(heading.group(1))
            last_heading_level = level
            if section_level is not None and (level == section_level or level == 1):
                section_sources, section_level = [], None
            if content_started and heading_level is not None and level <= heading_level:
                flush()
            if not content_started:
                heading_level = min(heading_level, level) if sources and heading_level is not None else level
            if sources:
                body.append(line)
            continue
        if value.startswith("【当前已确认摘要") or value.startswith("【AI OS"):
            flush()
            continue
        if sources and value and not value.startswith("用途与边界："):
            body.append(line)
            content_started = True
        elif sources and not value:
            body.append(line)
    flush()
    return result


def local_block_index(author_input: str) -> list[dict]:
    """给核验节点的机器索引：每个摘录块可用的路径和开头文字，免得模型在长材料里误挑别处的副本路径。"""
    index = []
    for block in local_source_blocks(author_input):
        head = " ".join(block["text"].split())[:60]
        index.append({"source_paths": block["source_paths"], "starts_with": head})
    return index


def _quote_key(text: str) -> str:
    # 引文要逐字，但中英文标点与空白的差异不改变归属，不因此判“无法定位”。
    return re.sub(r"[\s，,；;、。.：:！!？?“”\"‘’'（）()\-—–]+", "", text)


def local_quote_supported(author_input: str, source_path: str, quote: str) -> bool:
    path = source_path.strip().replace("\\", "/")
    # source_path 可保留来源原有行号，但不扩大到其他路径。
    path = re.sub(r":\d+(?:-\d+)?$", "", path)
    key = _quote_key(quote)
    return bool(key and any(path in block["source_paths"] and key in _quote_key(block["text"])
                            for block in local_source_blocks(author_input)))


def canonical_url(url: str) -> str:
    try:
        p = urlsplit(url.strip().strip("<>"))
        if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password:
            return ""
        return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/"), p.query, ""))
    except ValueError:
        return ""


def links(text: str) -> set[str]:
    # 除内联链接外也识别引用式链接和自动链接，避免绕过来源清单。
    found = re.findall(r"https?://[^\s<>\]）)]+", text)
    return {v for u in found if (v := canonical_url(u.rstrip(').,;，。；')))}


def record(source: dict, body: str) -> dict:
    excerpt = body[:config.SOURCE_TEXT_LIMIT]
    return {"title": source.get("title", ""), "source_url": canonical_url(source.get("url", "")),
            "evidence_text": excerpt, "content": source.get("content", ""),
            "fetched_at": source.get("fetched_at") or date.today().isoformat(),
            "published": source.get("published", ""),
            "source_status": "retrieved" if excerpt.strip() else "unverified",
            "sha256": hashlib.sha256(excerpt.encode()).hexdigest(),
            "truncated": len(body) > len(excerpt), "query": source.get("query", ""),
            "fresh": bool(source.get("fresh")), "wiki_status": source.get("wiki_status", "")}


def mechanical_issues(draft: str, polished: str, materials: list[dict], today: date | None = None) -> list[str]:
    issues = []
    today = today or date.today()
    sources = {canonical_url(m.get("source_url", "")): m for m in materials}
    for url in links(polished):
        m = sources.get(url)
        if not m or not m.get("evidence_text") or m.get("source_status") != "retrieved":
            issues.append(f"引用缺少已读取原文：{url}")
            continue
        try:
            age = (today - date.fromisoformat(m["fetched_at"])).days
            if age < 0 or age > config.SOURCE_FRESH_DAYS:
                issues.append(f"引用需重新核验时效：{url}")
        except (ValueError, KeyError):
            issues.append(f"引用缺少有效核验日期：{url}")
    if links(draft) != links(polished):
        issues.append("润色前后来源链接发生变化，需要复核")
    def numbers(text):
        text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
        text = re.sub(r"https?://\S+|^#+\s.*$", "", text, flags=re.M)
        return Counter(re.findall(r"(?<!\w)\d+(?:[.,]\d+)*(?:%|％)?", text))
    if numbers(draft) != numbers(polished):
        issues.append("润色前后数字发生变化，需要复核单位、范围和分母")
    return issues
