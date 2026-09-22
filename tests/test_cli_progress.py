"""真实本地子进程模拟 CLI JSONL；不启动 Claude/Codex，不连接模型。"""
import json
import subprocess
import sys

import pytest

import agent_cli as a
import llm


def fake_cli(monkeypatch, tmp_path, body):
    script = tmp_path / "fake_cli.py"
    script.write_text("import sys,json,time\nfrom pathlib import Path\n" + body, encoding="utf-8")
    monkeypatch.setattr(a, "command_for", lambda provider: [sys.executable, str(script)])
    processes = []
    original = a.subprocess.Popen
    def start(*args, **kwargs):
        process = original(*args, **kwargs)
        processes.append(process)
        return process
    monkeypatch.setattr(a.subprocess, "Popen", start)
    return processes


def test_live_jsonl_callback_precedes_process_completion(monkeypatch, tmp_path, capsys):
    release = tmp_path / "release"
    processes = fake_cli(monkeypatch, tmp_path,
        "prompt=sys.stdin.read()\n"
        "print(json.dumps({'type':'system','api_key':'SECRET'}),flush=True)\n"
        f"while not Path({str(release)!r}).exists(): time.sleep(0.01)\n"
        "print(json.dumps({'type':'assistant','message':{'id':'m','content':[{'type':'thinking','thinking':'HIDDEN'},{'type':'text','text':'OK'}]}}),flush=True)\n"
        "print('STDERR_SECRET',file=sys.stderr,flush=True)\n"
        "print(json.dumps({'type':'result','subtype':'success','result':'OK'}),flush=True)\n")
    seen = []
    def callback(event):
        seen.append(event)
        if event["events"] == 1:
            assert processes[0].poll() is None
            release.write_text("continue", encoding="utf-8")
    reply = a.invoke("claude", "", "PRIVATE_PROMPT", timeout=8, on_progress=callback, environment={})
    assert reply.text == "OK"
    assert seen[-1] == {"phase": "completed", "events": 3, "content_chars": 2, "tool_calls": 0, "tool_results": 0}
    assert processes[0].poll() == 0
    serialized = json.dumps(seen)
    assert not any(secret in serialized for secret in ("SECRET", "HIDDEN", "PRIVATE_PROMPT"))
    out = capsys.readouterr()
    assert "SECRET" not in out.out + out.err


def test_timeout_reaps_process_and_scrubs_prompt(monkeypatch, tmp_path):
    processes = fake_cli(monkeypatch, tmp_path, "sys.stdin.read()\ntime.sleep(60)\n")
    seen = []
    with pytest.raises(a.AgentError, match="timeout=0.3s") as caught:
        a.invoke("codex", "", "私密SECRET", timeout=0.3, on_progress=seen.append, environment={})
    assert processes[0].poll() is not None
    assert "输入UTF8字节=12" in str(caught.value)
    assert "SECRET" not in str(caught.value)
    assert seen[-1]["phase"] == "failed"


@pytest.mark.parametrize("events", [
    [{"type": "item.completed", "item": {"type": "agent_message", "text": "partial"}}],
    [{"type": "item.completed", "item": {"type": "agent_message", "text": "partial"}}, {"type": "turn.failed", "error": "SECRET"}],
])
def test_streamed_partial_or_error_never_passes(monkeypatch, tmp_path, events):
    fake_cli(monkeypatch, tmp_path, "sys.stdin.read()\n" +
        "\n".join("print(" + repr(json.dumps(event)) + ",flush=True)" for event in events) + "\n")
    seen = []
    with pytest.raises(a.AgentError) as caught:
        a.invoke("codex", "", "prompt", timeout=8, on_progress=seen.append, environment={})
    assert "SECRET" not in str(caught.value)
    assert seen[-1]["phase"] == "failed"


def test_stream_nonzero_exit_hides_stderr(monkeypatch, tmp_path):
    fake_cli(monkeypatch, tmp_path, "sys.stdin.read()\nprint('out of memory SECRET',file=sys.stderr)\nsys.exit(3)\n")
    with pytest.raises(a.AgentError) as caught:
        a.invoke("claude", "", "prompt", timeout=8, environment={})
    assert "诊断类别=memory" in str(caught.value)
    assert "SECRET" not in str(caught.value)


def test_large_stdin_and_stderr_do_not_deadlock(monkeypatch, tmp_path):
    fake_cli(monkeypatch, tmp_path, "sys.stderr.write('x'*200000)\nsys.stderr.flush()\ntext=sys.stdin.read()\n"
        "print(json.dumps({'type':'result','subtype':'success','result':str(len(text))}),flush=True)\n")
    assert a.invoke("claude", "", "P" * 500000, timeout=8, environment={}).text == "500000"


def test_old_completion_does_not_approve_new_incomplete_turn():
    events = [{"type": "item.completed", "item": {"type": "agent_message", "text": "old"}},
              {"type": "turn.completed"}, {"type": "turn.started"},
              {"type": "item.completed", "item": {"type": "agent_message", "text": "half-new"}}]
    with pytest.raises(a.AgentError):
        a.parse_output("codex", "\n".join(json.dumps(e) for e in events), "")


def test_claude_partial_final_and_multiple_tool_rounds_deduplicate():
    seen = []
    tracker = a._EventProgress("claude", seen.append)
    for i, text in [(1, "abc"), (2, "defg")]:
        tracker.accept({"type": "stream_event", "event": {"type": "message_start", "message": {"id": f"m{i}"}}})
        tracker.accept({"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "SECRET"}}})
        tracker.accept({"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}}})
        tracker.accept({"type": "stream_event", "event": {"type": "content_block_start", "content_block": {"type": "tool_use", "id": f"t{i}", "input": "SECRET"}}})
        tracker.accept({"type": "assistant", "message": {"id": f"m{i}", "content": [
            {"type": "text", "text": text}, {"type": "tool_use", "id": f"t{i}", "input": "SECRET"}]}})
        tracker.accept({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": f"t{i}", "content": "SECRET"}]}})
    tracker.accept({"type": "result", "subtype": "success", "result": "defg"})
    assert seen[-1]["content_chars"] == 7
    assert seen[-1]["tool_calls"] == seen[-1]["tool_results"] == 2
    assert seen[-1]["events"] == 13
    assert "SECRET" not in json.dumps(seen)


def test_codex_item_snapshots_do_not_double_count():
    tracker = a._EventProgress("codex", None)
    for kind, text in [("item.started", "a"), ("item.updated", "abc"), ("item.completed", "abc")]:
        tracker.accept({"type": kind, "item": {"id": "message", "type": "agent_message", "text": text}})
    for kind in ["item.started", "item.completed"]:
        tracker.accept({"type": kind, "item": {"id": "tool", "type": "mcp_tool_call", "arguments": "SECRET"}})
    assert tracker.content_chars == 3 and tracker.tool_calls == tracker.tool_results == 1


def test_process_activity_is_not_claimed_as_model_output(monkeypatch):
    p = llm._Progress("writer", "claude", mode="cli")
    p.last_data = 10
    monkeypatch.setattr(llm.time, "time", lambda: 45)
    p.got_cli_progress({"phase": "process_started", "events": 0, "content_chars": 0, "tool_calls": 0, "tool_results": 0})
    assert p.last_data == 10
    assert "尚未收到事件" in p._line() and "疑似卡住" not in p._line()
    assert p._snapshot("running")["content_chars"] == 0


def test_progress_heartbeat_marks_failed(tmp_path, monkeypatch):
    target = tmp_path / "heartbeat.json"
    monkeypatch.setenv("WRITING_HEARTBEAT_FILE", str(target))
    with pytest.raises(RuntimeError):
        with llm._Progress("reviewer", "codex", mode="cli") as progress:
            progress.got_cli_progress({"phase": "event", "events": 2, "content_chars": 3, "tool_calls": 0, "tool_results": 0})
            raise RuntimeError("fail")
    saved = json.loads(target.read_text(encoding="utf-8"))
    assert saved["phase"] == "failed" and saved["events"] == 2


def test_chat_and_tools_wire_real_event_callback(monkeypatch):
    monkeypatch.setattr(llm.config, "MOCK_LLM", False)
    monkeypatch.setitem(llm.config.ROLE_MODELS, "writer", ("claude", "model"))
    callbacks = []
    def invoke(*args, **kwargs):
        callback = kwargs["on_progress"]
        callbacks.append(callback)
        callback({"phase": "event", "events": 1, "content_chars": 2, "tool_calls": 0, "tool_results": 0})
        if "response_schema" in kwargs:
            return a.AgentReply('{"content":"OK","tool_calls":[]}', "model", "model")
        return a.AgentReply("<scratchpad>检查</scratchpad><result>OK</result>", "model", "model")
    monkeypatch.setattr(a, "invoke", invoke)
    assert llm.chat("writer", "system", "user").result == "OK"
    for _ in range(2):
        assert llm._cli_with_tools("claude", "model", [], [])[0].content == "OK"
    assert len(callbacks) == 3
