"""tools/memory.py 的测试集（不真实发 HTTP，替换 requests.post 为假实现）。

覆盖五条关键路径：纯 JSON 响应、SSE 响应、会话失效后重建重试、
服务不可用时的 fail-open、复核门 blocked 不注入。

用法：uv run python test_memory.py
退出码 0 = 全部通过；1 = 有用例失败。
"""
import json
import sys

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(errors="replace")

import requests

import config
from tools import memory

# handler 的参数名必须用 json=（匹配 requests.post 的调用签名），
# 所以 handler 内部要操作 JSON 模块时用这个别名，避免被参数遮蔽
_json = json


class FakeResp:
    """最小 requests.Response 替身：status_code/headers/text/json/raise_for_status。"""

    def __init__(self, payload=None, status=200, session_id="", sse_text=None):
        self.status_code = status
        self.headers = {"Content-Type": "application/json"}
        if session_id:
            self.headers["Mcp-Session-Id"] = session_id
        self._payload = payload or {"jsonrpc": "2.0", "id": 1, "result": {}}
        if sse_text is not None:
            self.headers["Content-Type"] = "text/event-stream"
            self.text = sse_text
        else:
            self.text = json.dumps(self._payload, ensure_ascii=False)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


def tool_msg(result_dict):
    return {"jsonrpc": "2.0", "id": 99, "result": {
        "content": [{"type": "text", "text": json.dumps(result_dict,
                                                       ensure_ascii=False)}],
        "isError": False}}


def run_case(name, handler, check):
    """每个用例换一批假响应，并重置客户端的会话状态（_session_id 是模块级缓存）。"""
    memory._session_id = None
    orig = requests.post
    requests.post = handler
    try:
        return name, check()
    finally:
        requests.post = orig
        memory._session_id = None


def case_json_ok():
    seen = {}

    def handler(url, json=None, headers=None, timeout=None):
        if json.get("method") == "initialize":
            return FakeResp(session_id="sid-1")
        if "notifications" in json.get("method", ""):
            return FakeResp()
        seen["sid"] = headers.get("Mcp-Session-Id")
        return FakeResp(tool_msg({"status": "ok", "block": "BLOCK内容"}))

    def check():
        ok = memory.context_block(query="测试") == "BLOCK内容"
        return ok and seen.get("sid") == "sid-1"  # 会话 id 要带回后续请求
    return run_case("纯JSON-正常返回", handler, check)


def case_sse():
    def handler(url, json=None, headers=None, timeout=None):
        if json.get("method") == "initialize":
            return FakeResp(session_id="sid-2")
        if "notifications" in json.get("method", ""):
            return FakeResp()
        payload = _json.dumps(tool_msg({"status": "ok", "block": "SSE块"}),
                              ensure_ascii=False)
        return FakeResp(sse_text=f"event: message\ndata: {payload}\n\n")

    return run_case("SSE响应-解析data行", handler,
                    lambda: memory.context_block() == "SSE块")


def case_session_rebuild():
    calls = {"init": 0, "tool": 0}

    def handler(url, json=None, headers=None, timeout=None):
        if json.get("method") == "initialize":
            calls["init"] += 1
            return FakeResp(session_id=f"sid-{calls['init']}")
        if "notifications" in json.get("method", ""):
            return FakeResp()
        calls["tool"] += 1
        if calls["tool"] == 1:
            return FakeResp(status=404)  # 第一次调用会话失效（比如服务重启过）
        return FakeResp(tool_msg({"status": "ok", "block": "重建后"}))

    def check():
        return memory.context_block() == "重建后" and calls["init"] == 2
    return run_case("会话失效-重建重试", handler, check)


def case_fail_open():
    def handler(url, json=None, headers=None, timeout=None):
        raise requests.ConnectionError("connection refused")

    def check():
        a = memory.context_block(query="x") == ""           # 不抛异常，返回空
        memory.wm_sync(goal="g", todos=[], turn=1)          # 不抛异常
        return a and memory.session_end("s", []) is None    # 不抛异常，返回 None
    return run_case("服务不可用-fail-open", handler, check)


def case_blocked():
    def handler(url, json=None, headers=None, timeout=None):
        if json.get("method") == "initialize":
            return FakeResp(session_id="sid-3")
        if "notifications" in json.get("method", ""):
            return FakeResp()
        return FakeResp(tool_msg({"status": "blocked", "pending_review_count": 2,
                                  "block": "不该注入的内容"}))

    return run_case("复核门blocked-不注入", handler,
                    lambda: memory.context_block() == "")


def main() -> int:
    if not config.MEMORY_ENABLED:
        print("MEMORY_ENABLED=0（或 mock 模式），本测试需要开启记忆客户端。")
        return 1
    cases = [case_json_ok, case_sse, case_session_rebuild, case_fail_open, case_blocked]
    failed = 0
    for fn in cases:
        try:
            name, ok = fn()
        except Exception as e:
            name, ok = fn.__name__, False
            print(f"[FAIL] {name}：异常 {type(e).__name__}: {e}")
            failed += 1
            continue
        print(f"[{'PASS' if ok else 'FAIL'}] {name}")
        failed += 0 if ok else 1
    print(f"\n{len(cases) - failed}/{len(cases)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
