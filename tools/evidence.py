"""记录证据与机械检查；不把链接存在或模型通过等同于事实证明。"""
import hashlib
import re
from collections import Counter
from datetime import date
from urllib.parse import urlsplit, urlunsplit

import config


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
