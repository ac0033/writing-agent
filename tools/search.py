"""Tavily 搜索封装，供 agent3 使用。"""
import config

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


def search(query: str, max_results: int = 5) -> list[dict]:
    """返回 [{title, url, content}, ...]。"""
    resp = _get_client().search(query, max_results=max_results)
    return [
        {"title": r.get("title", ""), "url": r.get("url", ""), "content": r.get("content", "")}
        for r in resp.get("results", [])
    ]
