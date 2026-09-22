"""CLI 失败边界与宿主管理工具协议；全部替身，不发真实请求。"""
import json
import subprocess

import pytest

import agent_cli
import llm


def test_codebuddy_array_keeps_actual_model():
    data = [{"type": "message", "providerData": {"model": "glm-5.3-flash"}},
            {"type": "result", "subtype": "success", "result": "OK"}]
    result = agent_cli.parse_output("codebuddy", json.dumps(data), "auto")
    assert result.text == "OK"
    assert result.actual_model == "glm-5.3-flash"


def test_claude_native_schema_result_and_command(monkeypatch):
    monkeypatch.setattr(agent_cli, 'command_for', lambda p: ['fake'])
    schema = {'type': 'object', 'properties': {'content': {'type': 'string'}}}
    def runner(cmd, **kwargs):
        assert json.loads(cmd[cmd.index('--json-schema') + 1]) == schema
        return subprocess.CompletedProcess(cmd, 0, json.dumps({'type': 'result',
            'subtype': 'success', 'result': '', 'structured_output': {'content': 'OK'}}), '')
    reply = agent_cli.invoke('claude', 'opus', 'OK', runner=runner, response_schema=schema)
    assert json.loads(reply.text) == {'content': 'OK'}


@pytest.mark.parametrize("events", [
    [{"type": "item.completed", "item": {"type": "agent_message", "text": "half"}}],
    [{"type": "turn.failed"}],
    [{"type": "result", "subtype": "error_max_turns", "result": "partial"}],
    [{"type": "result", "subtype": "success", "result": "OK", "permission_denials": ["Write"]}],
])
def test_partial_error_or_denial_never_counts_as_success(events):
    with pytest.raises(agent_cli.AgentError):
        agent_cli.parse_output("codex" if events[0]["type"].startswith(("turn", "item")) else "claude",
                               json.dumps(events), "requested")


def test_codex_requires_completion():
    output = '\n'.join(json.dumps(x) for x in [
        {"type": "thread.started", "thread_id": "id"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "OK"}},
        {"type": "turn.completed"}])
    result = agent_cli.parse_output("codex", output, "")
    assert result.text == "OK"
    assert result.actual_model == "未报告"


def test_prompt_via_stdin_and_tools_disabled(monkeypatch):
    monkeypatch.setattr(agent_cli, "command_for", lambda p: ["fake"])
    def runner(cmd, **kw):
        assert kw["input"] == "任务" * 20000
        assert kw["input"] not in cmd
        assert cmd[cmd.index("--tools") + 1] == ""
        assert "--strict-mcp-config" in cmd
        assert "--no-session-persistence" in cmd
        assert "--dangerously-skip-permissions" not in cmd
        return subprocess.CompletedProcess(cmd, 0, '{"type":"result","subtype":"success","result":"OK"}', "")
    assert agent_cli.invoke("claude", "opus", "任务" * 20000, runner=runner).text == "OK"


def test_tool_protocol_uses_only_declared_functions(monkeypatch):
    def reply(*args, **kwargs):
        return agent_cli.AgentReply('{"tool_calls":[{"name":"delete_files","arguments":{}}]}', "", "")
    monkeypatch.setattr(agent_cli, "invoke", reply)
    with pytest.raises(agent_cli.AgentError):
        llm._cli_with_tools("claude", "opus", [], [{"function": {"name": "search_corpus"}}])


def test_tool_protocol_round_trip(monkeypatch):
    def invoke(*args, **kwargs):
        schema = kwargs['response_schema']
        assert 'oneOf' not in schema
        assert schema['required'] == ['content', 'tool_calls']
        assert schema['properties']['tool_calls']['items']['properties']['name']['const'] == 'search_corpus'
        return agent_cli.AgentReply('{"tool_calls":[{"name":"search_corpus","arguments":{"query":"test"}}]}', 'opus', 'actual')
    monkeypatch.setattr(agent_cli, "invoke", invoke)
    message, label = llm._cli_with_tools("claude", "opus", [], [{"function": {"name": "search_corpus"}}])
    assert message.tool_calls[0].function.name == "search_corpus"
    assert json.loads(message.tool_calls[0].function.arguments) == {"query": "test"}
    assert "actual" in label


@pytest.mark.parametrize("fenced", [False, True])
def test_tool_protocol_fence_or_single_format_repair(monkeypatch, fenced):
    valid = '{"content":"<scratchpad>核查摘要</scratchpad><result>正文</result>"}'
    replies = ["```json\n" + valid + "\n```"] if fenced else ["非JSON正文", valid]
    prompts = []
    def invoke(*args, **kwargs):
        prompts.append(args[2])
        return agent_cli.AgentReply(replies.pop(0), "opus", "opus")
    monkeypatch.setattr(agent_cli, "invoke", invoke)
    result, _ = llm._cli_with_tools("claude", "opus", [], [])
    assert "正文" in result.content
    assert len(prompts) == (1 if fenced else 2)


def test_tool_protocol_repair_is_bounded(monkeypatch):
    calls = []
    def invoke(*args, **kwargs):
        calls.append(1)
        return agent_cli.AgentReply("仍不是JSON", "opus", "opus")
    monkeypatch.setattr(agent_cli, "invoke", invoke)
    with pytest.raises(agent_cli.AgentError, match="格式修复失败"):
        llm._cli_with_tools("claude", "opus", [], [])
    assert len(calls) == 2


def test_api_dotenv_does_not_override_cli_auth(monkeypatch):
    monkeypatch.setattr(agent_cli, "command_for", lambda p: ["fake"])
    monkeypatch.setenv("CODEBUDDY_BASE_URL", "https://wrong.invalid")
    def runner(cmd, **kw):
        assert "CODEBUDDY_BASE_URL" not in kw["env"]
        return subprocess.CompletedProcess(cmd, 0, '{"type":"result","subtype":"success","result":"OK"}', "")
    agent_cli.invoke("codebuddy", "hy4-preview", "OK", environment={}, runner=runner)


def test_researcher_receives_author_case_evidence(monkeypatch):
    """防止查询规划看到了案例，但正式资料整理时作者材料丢失。"""
    import graph
    monkeypatch.setattr(graph, "_wm_sync", lambda *a, **k: None)
    monkeypatch.setattr(graph, "_memory_prefix", lambda *a, **k: "")
    def run(role, node, system, user):
        assert "模拟渲染通过，但真实PDF接口缺依赖" in user
        return ("材料标题：测试\n来源：https://example.com/mock\n要点：保留案例的证据边界。",
                {"node": node, "model": "mock", "thinking": ""})
    monkeypatch.setattr(graph, "_run", run)
    result = graph.researcher({"topic": "产品开发", "thread_id": "test",
                              "user_idea": "模拟渲染通过，但真实PDF接口缺依赖"})
    assert len(result["materials"]) == 1


def test_researcher_missing_separator_cannot_merge_sources(monkeypatch):
    import graph
    monkeypatch.setattr(graph, "_wm_sync", lambda *a, **k: None)
    monkeypatch.setattr(graph, "_memory_prefix", lambda *a, **k: "")
    output = ("材料标题：已读\n来源：https://example.com/mock\n要点：第一来源事实。\n\n"
              "## 需求二\n材料标题：未读\n来源：https://unread.invalid/paper\n要点：另一来源数据。")
    monkeypatch.setattr(graph, "_run", lambda *a: (output, {"node":"agent3"}))
    result = graph.researcher({"topic":"测试", "thread_id":"test"})
    assert len(result["materials"]) == 1
    assert result["materials"][0]["content"] == "第一来源事实。"
    assert any("https://unread.invalid/paper" in gap for gap in result["research_gaps"])


def test_stylist_receives_author_and_passed_review_notes(monkeypatch):
    import graph
    monkeypatch.setattr(graph, "_wm_sync", lambda *a, **k: None)
    monkeypatch.setattr(graph, "_memory_prefix", lambda *a, **k: "")
    def run(role, node, system, user):
        assert "成本较低但并非免费" in user
        assert "未证实的个人经历需删除" in user
        return "润色稿", {"node":node}
    monkeypatch.setattr(graph, "_run", run)
    assert graph.stylist({'topic':'测试','draft':'初稿','user_idea':'成本较低但并非免费',
                         'review_verdict':'pass','review_comments':'未证实的个人经历需删除'})['polished'] == '润色稿'


def test_cli_failure_reports_category_without_raw_output(monkeypatch):
    monkeypatch.setattr(agent_cli, 'command_for', lambda p: ['fake'])
    def runner(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 3221226505, '', 'Out of memory account-secret')
    with pytest.raises(agent_cli.AgentError) as error:
        agent_cli.invoke('claude', 'opus', '任务', runner=runner)
    message = str(error.value)
    assert '诊断类别=memory' in message
    assert '输入UTF8字节=6' in message
    assert 'account-secret' not in message


@pytest.mark.skipif(agent_cli.os.name != "nt", reason="Codex Desktop 安装目录仅 Windows")
def test_codex_desktop_bundled_cli_found_when_not_on_path(monkeypatch, tmp_path):
    # 普通终端的 PATH 没有 codex；取安装目录里最近更新的一份，显式配置与 PATH 仍然优先。
    import os
    monkeypatch.delenv("CODEX_COMMAND_JSON", raising=False)
    monkeypatch.setattr(agent_cli.shutil, "which", lambda name: None)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    with pytest.raises(agent_cli.AgentError, match="找不到 codex"):
        agent_cli.command_for("codex")
    for index, name in enumerate(("old", "new")):
        target = tmp_path / "OpenAI/Codex/bin" / name / "codex.exe"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"")
        os.utime(target, (1000 + index, 1000 + index))
    assert agent_cli.command_for("codex") == [str(tmp_path / "OpenAI/Codex/bin/new/codex.exe")]
    with pytest.raises(agent_cli.AgentError, match="找不到 claude"):
        agent_cli.command_for("claude")
    monkeypatch.setattr(agent_cli.shutil, "which", lambda name: "C:/on-path/codex.cmd")
    assert agent_cli.command_for("codex") == ["C:/on-path/codex.cmd"]


def test_cli_usage_limit_is_diagnosed_without_raw_output(monkeypatch):
    # Codex 额度用尽时若只报 unknown，无法判断该等待还是换接入。
    monkeypatch.setattr(agent_cli, 'command_for', lambda p: ['fake'])
    def runner(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, '', "You've hit your usage limit. account-secret try again at Sep 27th")
    with pytest.raises(agent_cli.AgentError) as error:
        agent_cli.invoke('codex', '', '任务', runner=runner)
    assert '诊断类别=usage_limit' in str(error.value)
    assert 'account-secret' not in str(error.value)
