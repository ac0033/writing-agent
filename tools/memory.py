"""agent-memory 记忆服务的最小 MCP-over-HTTP 客户端（接入指南方式一）。

只封装本项目需要的四件事：memory_context（组装记忆块）、memory_wm_write
（工作记忆全量同步）、memory_session_end（会话收尾：归档+蒸馏+清理）、
可用性探测。会话日志供料用直传 conversation_json——本项目的运行日志不是
kimi-code wire.jsonl 格式，log_path 适配层不适用。

设计取舍：
- 手写 JSON-RPC over streamable-http，不引官方 mcp SDK：协议形状已实测
  （纯 JSON 请求响应，result.content[0].text 里是业务 JSON），项目代码
  全同步，SDK 的 async 接口会传染整条调用链。兼容 SSE 响应作为兜底。
- 全程 fail-open：记忆是增强不是依赖，服务挂了/慢了绝不能中断写作主流程。
  所有对外函数捕获一切异常，打印一行警告后返回安全默认值。
  （与服务端的 fail-closed 不冲突：服务端保证库里内容的可信，
  消费方保证自己不被拖死。）
- 人工复核相关状态不自动处理：复核门 blocked 只报告不放行（不自动
  acknowledge_pending），session_end 的 pending_review 只透出不裁决。
"""
import json
import itertools
import threading
import time

import requests

import config
from log import log

# 进程内复用同一个 MCP 会话（initialize 一次，后续调用带 Mcp-Session-Id）
_session_id: str | None = None
_ids = itertools.count(1)
_rpc_lock = threading.RLock()
_retry_after = 0.0


class MemoryClientError(Exception):
    """记忆服务调用失败（网络/协议/服务端报错），调用方据此 fail-open。"""


def _ensure_session() -> None:
    """惰性建立 MCP 会话：initialize 拿 Mcp-Session-Id，再发 initialized 通知。"""
    global _session_id
    if _session_id:
        return
    r = requests.post(
        config.MEMORY_MCP_URL,
        json={"jsonrpc": "2.0", "id": next(_ids), "method": "initialize",
              "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                         "clientInfo": {"name": "writing-agent", "version": "0.1"}}},
        headers={"Accept": "application/json, text/event-stream"},
        timeout=config.MEMORY_CONTEXT_TIMEOUT,
    )
    r.raise_for_status()
    _session_id = r.headers.get("Mcp-Session-Id", "")
    if "error" in _parse_response(r):
        _session_id = None
        raise MemoryClientError("记忆服务初始化失败")
    requests.post(
        config.MEMORY_MCP_URL,
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers={"Accept": "application/json, text/event-stream",
                 **({"Mcp-Session-Id": _session_id} if _session_id else {})},
        timeout=config.MEMORY_CONTEXT_TIMEOUT,
    ).raise_for_status()


def _parse_response(r: requests.Response) -> dict:
    """从响应里取 JSON-RPC 消息。正常是纯 JSON；服务端走 SSE 时解析 data: 行。"""
    if "text/event-stream" in r.headers.get("Content-Type", ""):
        for line in r.text.splitlines():
            if line.startswith("data:"):
                msg = json.loads(line[5:].strip())
                if "result" in msg or "error" in msg:
                    return msg
        raise MemoryClientError(f"SSE 响应中没有 JSON-RPC 结果：{r.text[:200]}")
    return r.json()


def _post(payload: dict) -> dict:
    """发一个 JSON-RPC 请求并返回结果消息。会话失效（404/410）时重建一次。"""
    global _session_id
    _ensure_session()
    timeout = config.MEMORY_TIMEOUT if payload.get("params", {}).get("name") == "memory_session_end" else config.MEMORY_CONTEXT_TIMEOUT
    headers = {"Accept": "application/json, text/event-stream"}
    if _session_id:
        headers["Mcp-Session-Id"] = _session_id
    r = requests.post(config.MEMORY_MCP_URL, json=payload, headers=headers,
                      timeout=timeout)
    if r.status_code in (404, 410):  # 服务重启会话丢失：重建会话重试一次
        _session_id = None
        _ensure_session()
        headers.pop("Mcp-Session-Id", None)
        if _session_id:
            headers["Mcp-Session-Id"] = _session_id
        r = requests.post(config.MEMORY_MCP_URL, json=payload, headers=headers,
                          timeout=timeout)
    r.raise_for_status()
    return _parse_response(r)


def call_tool(name: str, arguments: dict) -> dict:
    global _retry_after
    with _rpc_lock:
        if time.monotonic() < _retry_after:
            raise MemoryClientError("记忆服务暂不可用，冷却期内跳过重试")
        try:
            result = _call_tool(name, arguments)
            _retry_after = 0
            return result
        except (requests.RequestException, MemoryClientError, ValueError):
            _retry_after = time.monotonic() + config.MEMORY_RETRY_COOLDOWN
            raise


def _call_tool(name: str, arguments: dict) -> dict:
    """调一个 MCP tool，返回业务结果（result.content[0].text 解析出的 dict）。"""
    msg = _post({"jsonrpc": "2.0", "id": next(_ids), "method": "tools/call",
                 "params": {"name": name, "arguments": arguments}})
    if "error" in msg:
        raise MemoryClientError(f"{name} 协议层错误：{msg['error']}")
    result = msg.get("result", {})
    if result.get("isError"):
        text = "".join(c.get("text", "") for c in result.get("content", []))
        raise MemoryClientError(f"{name} 执行失败：{text[:300]}")
    text = "".join(c.get("text", "") for c in result.get("content", [])
                   if c.get("type") == "text")
    return json.loads(text) if text.strip() else {}


# ---------- 对外封装（全部 fail-open） ----------

def available() -> bool:
    """服务是否在线（供启动时提示用；失败不抛异常）。"""
    if not config.MEMORY_ENABLED:
        return False
    try:
        _ensure_session()
        return True
    except Exception:
        return False


def context_block(query: str | None = None, current_turn: int = 0, scope: str | None = None) -> str:
    """取 memory_context 组装块（常驻画像 + 工作记忆 + 按需召回）。

    复核门 blocked（复核队列积压）时不放行不注入，只打印提示——
    按接入指南，是否 acknowledge_pending 必须由人决定。
    """
    if not config.MEMORY_ENABLED:
        return ""
    args = {"scope": scope or config.MEMORY_SCOPE, "current_turn": current_turn}
    if query:
        args["query"] = query
    try:
        r = call_tool("memory_context", args)
    except Exception as e:
        log(f"[memory] ⚠️ 记忆服务不可用，本次不注入记忆（{type(e).__name__}: {e}）")
        return ""
    if r.get("status") == "blocked":
        log(f"[memory] ⚠️ 复核队列积压 {r.get('pending_review_count', '?')} 条，"
              "本次未注入记忆。请在 Kimi Code 会话中用 memory_review_list 处理。")
        return ""
    return r.get("block", "")


def wm_sync(goal: str, todos: list[dict], decisions: list[str] | None = None,
            notes: list[str] | None = None, turn: int = 0, scope: str | None = None) -> None:
    """全量替换式同步工作记忆（memory_wm_write 是全量替换语义，不是合并）。"""
    if not config.MEMORY_ENABLED:
        return
    try:
        call_tool("memory_wm_write", {
            "scope": scope or config.MEMORY_SCOPE,
            "goal": goal,
            "decisions": decisions or [],
            "todos": todos,
            "notes": notes or [],
            "turn_watermark": turn,
        })
    except Exception as e:
        log(f"[memory] ⚠️ 工作记忆同步失败（不影响流程）：{type(e).__name__}: {e}")


def session_end(session_id: str, conversation: list[dict], scope: str | None = None) -> dict | None:
    """会话收尾：归档 + 联合蒸馏 + 清理已完成待办。返回服务端结果供调用方透出。"""
    if not config.MEMORY_ENABLED:
        return None
    try:
        return call_tool("memory_session_end", {
            "scope": scope or config.MEMORY_SCOPE,
            "session_id": session_id,
            "conversation_json": json.dumps(conversation, ensure_ascii=False),
        })
    except Exception as e:
        log(f"[memory] ⚠️ 记忆收尾失败（不影响稿子保存）：{type(e).__name__}: {e}")
        return None
