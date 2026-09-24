"""额度来源、失败诊断和接入显示要与实际一致；全部替身，不发真实请求。"""
import json
import subprocess
import time

import pytest

import agent_cli
import ai_os_connection as c


FUTURE = 1999999999


def event(status="allowed", five=0.71, seven=0.14, kind="five_hour"):
    return {"type": "rate_limit_event", "rate_limit_info": {"status": status, "resetsAt": FUTURE, "rateLimitType": kind,
        "overageStatus": "rejected", "isUsingOverage": False, "accountEmail": "secret@example.com",
        "unifiedWindows": {"five_hour": {"utilization": five, "resetsAt": FUTURE},
                           "seven_day": {"utilization": seven, "resetsAt": FUTURE}}}}


def test_claude_cli_event_becomes_fresh_quota_without_account_fields(tmp_path):
    target = tmp_path / "quota.json"
    c.record_claude_rate_limit(event()["rate_limit_info"], target)
    text = target.read_text(encoding="utf-8")
    assert "secret" not in text and "overage" not in text
    quota = c.probe_claude(path=target)
    assert quota.remaining_percent == 29 and quota.permits()
    assert quota.reason == "Claude Code 最低窗口剩余 29%（五小时窗口已用 71%，七天窗口已用 14%）"


def test_rejected_window_counts_as_exhausted_even_if_percentage_lags(tmp_path):
    target = tmp_path / "quota.json"
    c.record_claude_rate_limit(event(status="rejected", five=0.7)["rate_limit_info"], target)
    quota = c.probe_claude(path=target)
    assert quota.remaining_percent == 0 and not quota.permits()


def test_extra_reported_window_lowers_the_minimum(tmp_path):
    # 只看五小时和七天两个总窗口会高估：按模型的窗口先用尽时也必须拒绝。
    payload = {"observed_at": time.time(), "rate_limits": {
        "five_hour": {"used_percentage": 30, "resets_at": FUTURE}, "seven_day": {"used_percentage": 13, "resets_at": FUTURE},
        "seven_day_opus": {"used_percentage": 96, "resets_at": FUTURE}}}
    quota = c.parse_claude_quota(payload)
    assert quota.remaining_percent == 4 and not quota.permits()
    target = tmp_path / "quota.json"
    c.capture_claude_quota({"rate_limits": payload["rate_limits"], "session_id": "x"}, target)
    assert "seven_day_opus" in json.loads(target.read_text(encoding="utf-8"))["rate_limits"]


def test_stream_progress_records_claude_event_only_for_claude(monkeypatch):
    seen = []
    monkeypatch.setattr(agent_cli, "_record_claude_rate_limit", seen.append)
    agent_cli._EventProgress("claude", None).accept(event())
    agent_cli._EventProgress("codebuddy", None).accept(event())
    assert len(seen) == 1 and seen[0]["unifiedWindows"]["five_hour"]["utilization"] == 0.71


def test_mock_mode_never_writes_the_real_quota_file(monkeypatch, tmp_path):
    target = tmp_path / "quota.json"
    monkeypatch.setattr(c, "DEFAULT_CLAUDE_QUOTA_FILE", target)
    monkeypatch.delenv("WRITING_CLAUDE_QUOTA_FILE", raising=False)
    c.record_claude_rate_limit(event()["rate_limit_info"])
    assert not target.exists()


def test_stale_file_triggers_one_live_query_and_refreshes(monkeypatch, tmp_path):
    target = tmp_path / "quota.json"
    target.write_text(json.dumps({"observed_at": time.time() - 3600, "rate_limits": {
        "five_hour": {"used_percentage": 1, "resets_at": FUTURE}, "seven_day": {"used_percentage": 1, "resets_at": FUTURE}}}), encoding="utf-8")
    calls = []
    monkeypatch.setattr(c, "query_claude_rate_limit", lambda: calls.append(1) or event(five=0.95)["rate_limit_info"])
    assert c.probe_claude(path=target).remaining_percent is None and not calls   # 显式路径且未要求实时查询：保持只读
    quota = c.probe_claude(path=target, live=True)
    assert calls == [1] and quota.remaining_percent == 5 and not quota.permits()   # 5% 低于 10% 门槛
    monkeypatch.setattr(c, "query_claude_rate_limit", lambda: None)
    target.write_text("{}", encoding="utf-8")
    assert c.probe_claude(path=target, live=True).reason == "无法读取 Claude Code 实时额度；禁止接入"


def test_normal_claude_output_is_not_diagnosed_as_usage_limit(monkeypatch):
    # Claude 每次输出都带 rate_limit_event 字样，普通失败不能据此误报为 usage_limit。
    monkeypatch.setattr(agent_cli, "command_for", lambda p: ["fake"])
    stdout = "\n".join(json.dumps(x) for x in (event(), {"type": "result", "subtype": "error_during_execution",
        "is_error": True, "result": "API Error: 529 overloaded account-secret"}))
    with pytest.raises(agent_cli.AgentError) as error:
        agent_cli.invoke("claude", "opus", "任务", runner=lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, stdout, ""))
    message = str(error.value)
    assert "usage_limit" not in message and "result_error_during_execution" in message and "api_529" in message
    assert "account-secret" not in message
    rejected = json.dumps(event(status="rejected"))
    assert agent_cli.failure_categories(rejected) == ["usage_limit"]


def test_display_names_follow_the_actual_connection():
    selected = c.ConnectionSelection("claude", "", reason="x")
    assert c.describe_selection(selected) == "Claude Code / CLI 默认模型"
    assert c.describe_selection(c.ConnectionSelection("deepseek", "deepseek-v4-pro")) == "DeepSeek API / deepseek-v4-pro"
    assert "Codex → Claude Code → DeepSeek API" in c.describe_settings(c.ConnectionSettings())
    assert c.describe_settings(c.ConnectionSettings(provider="claude")) == "Claude Code / CLI 默认模型"
    assert c.describe_settings(c.ConnectionSettings(provider="codex", model="gpt-6-sol")) == "Codex / gpt-6-sol"
    manager = c.ConnectionManager(codex_probe=lambda: c.QuotaStatus("codex", 0, time.time(), "Codex 报告已达到额度上限"),
                                  claude_probe=lambda: c.QuotaStatus("claude", 29, time.time(), "Claude Code 最低窗口剩余 29%"))
    chosen = manager.resolve(c.ConnectionSettings())
    assert chosen.provider == "claude" and chosen.reason == "Codex 报告已达到额度上限；Claude Code 最低窗口剩余 29%"
    with pytest.raises(c.ConnectionError, match="禁止接入 Codex"):
        manager.resolve(c.ConnectionSettings(provider="codex"))
