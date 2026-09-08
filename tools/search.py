"""Tavily 搜索封装，供 agent3 使用。"""
import config
from datetime import date
from log import log

_client = None


def _get_client():
    global _client
    if _client is None:
        if not config.TAVILY_API_KEY:
            raise RuntimeError(
                f"未找到 TAVILY_API_KEY，请在 {config.ENV_PATH} 中追加 "
                "TAVILY_API_KEY=tvly-...（去 tavily.com 注册，免费额度约 1000 次/月）。"
            )
        from tavily import TavilyClient
        _client = TavilyClient(api_key=config.TAVILY_API_KEY)
    return _client


def search(query: str, max_results: int = 5, fresh: bool = False) -> list[dict]:
    """返回 [{title, url, content}, ...]。"""
    if config.MOCK_LLM:
        return []
    try:
        resp = _get_client().search(query, max_results=max_results, search_depth="advanced",
                                    include_raw_content="text", time_range="month" if fresh else None)
    except Exception as exc:
        log(f"[search] 搜索失败，保留证据缺口：{type(exc).__name__}")
        return []
    return [
        {"title": r.get("title", ""), "url": r.get("url", ""), "content": r.get("content", ""),
         "raw_content": r.get("raw_content") or "", "published": r.get("published_date") or "",
         "fetched_at": date.today().isoformat(), "query": query, "fresh": fresh}
        for r in resp.get("results", [])
    ]


def extract_sources(urls: list[str]) -> dict[str, str]:
    """只读取已发现的 URL，失败不制造替代正文。"""
    if config.MOCK_LLM or not urls:
        return {}
    try:
        resp = _get_client().extract(urls=urls, format="text", extract_depth="advanced")
        return {r["url"]: r.get("raw_content") or "" for r in resp.get("results", [])
                if r.get("url") in urls}
    except Exception as exc:
        log(f"[search] 原文读取失败，资料按未核验处理：{type(exc).__name__}")
        return {}
