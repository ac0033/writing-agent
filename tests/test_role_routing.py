"""专业节点接入由 AI OS 自行解析：配置分工 → 任务级覆盖 → 额度核验 → 回退链；切换可见、在途节点不受影响。"""
import time

import pytest

import ai_os_connection as c
import config


def quota(provider, remaining):
    return c.QuotaStatus(provider, remaining, time.time(), f"{c.display_name(provider)} 最低窗口剩余 {remaining}%", time.time() + 3600)


def manager(codex, claude, calls=None):
    calls = calls if calls is not None else []
    return c.ConnectionManager(codex_probe=lambda: calls.append("codex") or quota("codex", codex),
                               claude_probe=lambda: calls.append("claude") or quota("claude", claude))


@pytest.fixture(autouse=True)
def real_mode(monkeypatch):
    monkeypatch.setattr(config, "MOCK_LLM", False)
    monkeypatch.setattr(config, "ROLE_FALLBACK", "auto")
    monkeypatch.setitem(config.PROVIDERS["deepseek"], "api_key", "test-deepseek-key")
    c.clear_quota_cache()
    yield
    c.clear_quota_cache()


def test_strict_mode_uses_configuration_without_probing(monkeypatch):
    monkeypatch.setattr(config, "ROLE_FALLBACK", "strict")
    monkeypatch.setitem(config.ROLE_MODELS, "reviewer", ("codex", ""))
    boom = c.ConnectionManager(codex_probe=lambda: pytest.fail("strict 不能探测额度"),
                               claude_probe=lambda: pytest.fail("strict 不能探测额度"))
    selected = c.resolve_role("reviewer", manager=boom)
    assert (selected.provider, selected.model, selected.role) == ("codex", "", "reviewer")


def test_insufficient_codex_falls_back_to_claude_and_reports_visibly(monkeypatch):
    monkeypatch.setitem(config.ROLE_MODELS, "reviewer", ("codex", ""))
    seen, recorded = [], []
    with c.use_connection(c.ConnectionSettings(), on_route=seen.append), c.use_role_settings(None, recorded.append):
        selected = c.resolve_role("reviewer", manager=manager(codex=2, claude=60))
    assert selected.provider == "claude" and selected.role == "reviewer"
    assert "Codex 最低窗口剩余 2%" in selected.reason and "回退第 1 顺位" in selected.reason
    assert seen == [selected] and recorded == [selected]


def test_both_clis_unavailable_falls_back_to_deepseek_api_with_fallback_model(monkeypatch):
    monkeypatch.setitem(config.ROLE_MODELS, "writer", ("claude", "claude-opus-5"))
    selected = c.resolve_role("writer", manager=manager(codex=3, claude=1))
    assert selected.provider == "deepseek" and selected.model == config.ROLE_FALLBACK_API_MODEL
    assert selected.api_key == "test-deepseek-key" and "接入 DeepSeek API" in selected.reason
    assert c.describe_selection(selected) == f"DeepSeek API / {config.ROLE_FALLBACK_API_MODEL}"


def test_no_route_at_all_raises_and_keeps_reasons(monkeypatch):
    monkeypatch.setitem(config.ROLE_MODELS, "writer", ("claude", ""))
    monkeypatch.setitem(config.PROVIDERS["deepseek"], "api_key", "")
    with pytest.raises(c.ConnectionError, match="没有可用接入.*未配置 API key"):
        c.resolve_role("writer", manager=manager(codex=0, claude=0))


def test_task_override_takes_precedence_and_is_read_from_snapshot(monkeypatch):
    monkeypatch.setitem(config.ROLE_MODELS, "reviewer", ("codex", ""))
    holder = {"snapshot": {"reviewer": {"provider": "claude", "model": "claude-fable-5-1"}}}
    with c.use_role_settings(lambda: holder["snapshot"]):
        assert c.role_target("reviewer") == ("claude", "claude-fable-5-1")
        selected = c.resolve_role("reviewer", manager=manager(codex=90, claude=60))
        assert (selected.provider, selected.model) == ("claude", "claude-fable-5-1")
        # 快照未刷新前改任务设置不影响解析：在途节点沿用开始时的分工。
        holder["snapshot"] = {}
        assert c.role_target("reviewer") == ("codex", "")
    assert c.role_target("reviewer") == ("codex", "")


def test_quota_probe_result_is_reused_briefly_within_a_node(monkeypatch):
    monkeypatch.setitem(config.ROLE_MODELS, "writer", ("claude", "claude-opus-5"))
    calls = []
    m = manager(codex=90, claude=60, calls=calls)
    for _ in range(3):
        assert c.resolve_role("writer", manager=m).provider == "claude"
    assert calls == ["claude"]
    monkeypatch.setattr(config, "ROLE_QUOTA_CACHE_S", 0)
    c.resolve_role("writer", manager=m)
    assert calls == ["claude", "claude"]


def test_orchestrator_is_not_resolved_here():
    with pytest.raises(ValueError):
        c.resolve_role("orchestrator")


def test_llm_chat_uses_resolved_route_for_specialists(monkeypatch):
    import agent_cli
    import llm
    monkeypatch.setitem(config.ROLE_MODELS, "reviewer", ("codex", ""))
    fixed = manager(codex=1, claude=70)
    monkeypatch.setattr(c, "ConnectionManager", lambda *a, **k: fixed)
    invoked = []
    def invoke(provider, model, prompt, **kwargs):
        invoked.append((provider, model))
        return agent_cli.AgentReply("<scratchpad>核查</scratchpad><result>VERDICT: PASS</result>", model, "claude-x")
    monkeypatch.setattr(agent_cli, "invoke", invoke)
    result = llm.chat("reviewer", "system", "user")
    assert invoked == [("claude", "")] and result.result == "VERDICT: PASS"
    assert result.model.startswith("Claude Code / claude-x")


def test_runner_refreshes_role_snapshot_only_at_node_boundaries(monkeypatch):
    from service import runner
    seen = []
    task = {"role_settings": {"writer": {"provider": "claude", "model": "m1"}}, "timeline": []}
    class FakeGraph:
        def stream(self, first_input, cfg, stream_mode):
            seen.append(("node-start", dict(snapshot["snapshot"])))
            task["role_settings"] = {"writer": {"provider": "deepseek", "model": ""}}  # 用户在节点执行中改分工
            yield {"writer": {}}
            seen.append(("node-start", dict(snapshot["snapshot"])))
            yield {"reviewer": {}}
        def get_state(self, cfg):
            raise RuntimeError("no state")
    snapshot = {"snapshot": {}}
    def boundary():
        snapshot["snapshot"] = dict(task["role_settings"])
    runner._stream_graph(task, FakeGraph(), None, {}, lambda t: None, boundary=boundary)
    assert seen[0][1] == {"writer": {"provider": "claude", "model": "m1"}}
    assert seen[1][1] == {"writer": {"provider": "deepseek", "model": ""}}
    assert [t["node"] for t in task["timeline"]] == ["writer", "reviewer"]


def test_task_manager_configure_roles_validates_and_persists(tmp_path):
    from service.writing_server import TaskManager
    m = TaskManager(tmp_path / "tasks.json", on_saved=None)
    m.tasks["t"] = {"task_id": "t", "thread_id": "t", "status": "running", "pipeline_version": "v2"}
    result = m.configure_roles("t", {"reviewer": {"provider": "claude", "model": "claude-fable-5-1"}, "writer": None})
    assert result["role_settings"] == {"reviewer": {"provider": "claude", "model": "claude-fable-5-1"}}
    assert m.status("t")["role_settings"] == result["role_settings"]
    assert "下一节点边界" in result["applies"]
    m.configure_roles("t", {"reviewer": {"provider": ""}})
    assert m.status("t")["role_settings"] == {}
    for bad in ({"editor": {"provider": "claude"}}, {"reviewer": {"provider": "openai"}}, {}, "x"):
        with pytest.raises(ValueError):
            m.configure_roles("t", bad)
    with pytest.raises(ValueError, match="未知任务"):
        m.configure_roles("missing", {"reviewer": {"provider": "claude"}})


def test_drive_records_every_route_into_task_for_display(tmp_path, monkeypatch):
    """runner 把每次实际接入写进 task['routes']，页面据此同步显示；mock 图不发请求。"""
    from service import runner
    task = {"task_id": "r", "thread_id": "r", "pipeline_version": "v2", "topic": "t", "idea": "i", "status": "pending",
            "role_settings": {}}
    captured = {}
    def fake_stream(task_, graph, first_input, cfg, persist, boundary=None):
        boundary()
        c._notify_route(c.ConnectionSelection("claude", "claude-x", role="reviewer", reason="回退"))
        captured["snapshot"] = c.role_target("reviewer")
        raise RuntimeError("stop here")
    monkeypatch.setattr(runner, "_stream_graph", fake_stream)
    monkeypatch.setitem(config.ROLE_MODELS, "reviewer", ("codex", ""))
    task["role_settings"] = {"reviewer": {"provider": "claude", "model": "claude-fable-5-1"}}
    runner.drive(task, None, lambda t: None, checkpoint_db=tmp_path / "cp.sqlite")
    assert task["status"] == "failed"
    assert task["routes"][-1]["role"] == "reviewer" and task["routes"][-1]["provider"] == "claude"
    assert captured["snapshot"] == ("claude", "claude-fable-5-1")
