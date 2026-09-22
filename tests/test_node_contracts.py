"""专业节点严格输出验收；只使用虚构数据与本地替身。"""
import hashlib
from types import SimpleNamespace

import pytest

import graph


def test_empty_contract_gets_one_repair_and_records_calls(monkeypatch):
    answers = iter([SimpleNamespace(parsed_ok=True, result="", thinking="", model="fake"),
                    SimpleNamespace(parsed_ok=True, result="内容", thinking="", model="fake")])
    monkeypatch.setattr(graph.llm, "chat", lambda *args: next(answers))
    text, entry = graph._run("reviewer", "test", "system", "user")
    assert text == "内容"
    assert entry["llm_calls"] == 2 and entry["contract_repairs"] == 1


def test_loaded_skill_fingerprints_match_actual_contents():
    from pathlib import Path
    prompt = graph._load_prompt("agent4_reviewer.md")
    entries = graph._prompt_files.get()
    assert entries and any(e["kind"] == "project_adaptation" for e in entries)
    for entry in entries:
        body = Path(entry["path"]).read_text(encoding="utf-8")
        assert hashlib.sha256(body.encode()).hexdigest() == entry["sha256"]
        assert body in prompt


@pytest.mark.parametrize("extra", ["\nFACTS: FAIL", "\nREADING: PASS", "\nVERDICT: FAIL"])
def test_contradictory_or_duplicate_verdict_not_pass(monkeypatch, extra):
    monkeypatch.setattr(graph, "_wm_sync", lambda *args: None)
    monkeypatch.setattr(graph, "_memory_prefix", lambda *args, **kwargs: "")
    monkeypatch.setattr(graph, "_run", lambda *args: (
        "VERDICT: PASS\nINTENT: PASS\nFACTS: PASS\nREADING: PASS" + extra, {}))
    result = graph.reviewer(dict(topic="测试", outline="纲", draft="稿", pipeline_version="v2"))
    assert result["review_verdict"] == "fail"


@pytest.mark.parametrize("claims", [[], [{"kind": "author", "assessment": "supported"}]])
def test_empty_or_unexplained_claims_cannot_publish(monkeypatch, claims):
    import json
    monkeypatch.setattr(graph, "_run", lambda *args: (
        json.dumps(dict(verdict="pass", claims=claims, issues=[])), {}))
    result = graph.final_check(dict(polished="作者观点", draft="作者观点", user_idea="作者观点",
        review_verdict="pass", pipeline_version="v2"))
    assert result["publication_ready"] is False
    assert result["quality_issues"]


def test_local_quote_bound_to_specific_source_and_continuous_block():
    from tools.evidence import local_quote_supported
    material = """### 项目甲
定位：D:/project-a/log.md:3-5
保存副本：D:/copies/combined.md:10-12
用途与边界：历史状态。
甲项目通过12项测试。
### 项目乙
来源：D:/project-b/log.md:4
乙项目仍有8项失败。
### 未注明来源的评论
未来所有项目都会完全通过。
"""
    assert local_quote_supported(material, "D:/project-a/log.md", "甲项目通过12项测试")
    assert local_quote_supported(material, "D:/copies/combined.md", "甲项目通过12项测试")
    assert not local_quote_supported(material, "D:/project-a/log.md", "乙项目仍有8项失败")
    assert not local_quote_supported(material, "D:/project-b/log.md", "未来所有项目都会完全通过")
    assert not local_quote_supported("仅提到 D:/project-a/log.md\n甲项目通过12项测试", "D:/project-a/log.md", "甲项目通过12项测试")


def test_nested_source_document_headings_preserve_provenance():
    from tools.evidence import local_quote_supported
    source = "### 项目原文片段\n定位：D:/project/status.md，从开头截取\n\n### 当前目标\n\n#### 当前事实\n已交付442页。\n\n#### 已知限制\n仍有待复核。\n### 其他项目\n来源：D:/other/log.md\n其他事实。"
    assert local_quote_supported(source, "D:/project/status.md", "已交付442页")
    assert local_quote_supported(source, "D:/project/status.md", "仍有待复核")
    assert not local_quote_supported(source, "D:/project/status.md", "其他事实")


def test_style_references_reused_by_writer_and_stylist(monkeypatch):
    reference = dict(source="author/article.md", arguments={"source": "author/article.md", "start_line": 3},
        content="来源：author/article.md\n行范围：3-8\n\n冻结的连续范文", sha256="test")
    captured = []
    monkeypatch.setattr(graph, "_wm_sync", lambda *args: None)
    monkeypatch.setattr(graph, "_memory_prefix", lambda *args, **kwargs: "")
    def run(role, node, system, user):
        captured.append(user)
        return "正文", {"style_references": [dict(content="新检索结果不能覆盖原参考")]}
    monkeypatch.setattr(graph, "_run", run)
    state = dict(topic="测试", user_idea="观点", outline="纲", draft="稿", style_references=[reference])
    assert graph.writer(state)["style_references"] == [reference]
    assert graph.stylist(state)["style_references"] == [reference]
    assert all(reference["content"] in user for user in captured)


def test_final_check_receives_revision_issue_registry(monkeypatch):
    captured = []
    def run(*args):
        captured.append(args[-1])
        return '{"verdict":"fail","claims":[],"issues":[]}', {}
    monkeypatch.setattr(graph, "_run", run)
    graph.final_check(dict(polished="稿", revision_feedback="[ISSUE:f17] 缺少支持证据"))
    assert "[ISSUE:f17]" in captured[0]


def test_actual_corpus_tool_read_saved_with_range_and_fingerprint(monkeypatch):
    import json
    call = SimpleNamespace(id="c1", function=SimpleNamespace(name="read_corpus",
        arguments=json.dumps(dict(source="author/a.md", start_line=3, max_lines=8))))
    first = SimpleNamespace(tool_calls=[call], content="", model_dump=lambda **kwargs:
        dict(role="assistant", content="", tool_calls=[]))
    second = SimpleNamespace(tool_calls=[], content="<scratchpad>核对</scratchpad><result>方案</result>",
        model_dump=lambda **kwargs: dict(role="assistant", content="方案"))
    replies = iter([first, second])
    monkeypatch.setattr(graph.llm, "chat_with_tools", lambda *args: (next(replies), "fake"))
    body = "来源：author/a.md\n行范围：3-10 / 40\n用途：写法参考\n\n连续正文"
    text, entry = graph._run_tool_loop("architect", "test", "system", "user", [],
        {"read_corpus": lambda **kwargs: body})
    reference = entry["style_references"][0]
    assert reference["content"] == body
    assert reference["arguments"]["start_line"] == 3
    assert reference["sha256"] == hashlib.sha256(body.encode()).hexdigest()
    assert text == "方案"


def test_both_auditors_receive_actual_style_read_evidence(monkeypatch):
    reference = {"source": "author/article.md", "content": "行范围：3-8\n冻结的作者连续范文", "sha256": "test"}
    captured = []
    monkeypatch.setattr(graph, "_wm_sync", lambda *a: None)
    monkeypatch.setattr(graph, "_memory_prefix", lambda *a, **k: "")
    def run(role, node, system, user):
        captured.append(user)
        return ('VERDICT: FAIL\nINTENT: PASS\nFACTS: FAIL\nREADING: PASS' if role == "reviewer"
                else '{"verdict":"fail","claims":[],"issues":[]}'), {}
    monkeypatch.setattr(graph, "_run", run)
    current = {"topic": "测试", "outline": "纲", "draft": "稿", "polished": "润色稿", "style_references": [reference]}
    graph.reviewer(current)
    graph.final_check(current)
    assert len(captured) == 2
    assert all(reference["content"] in message for message in captured)


def test_ai_os_complete_json_repair_is_short_and_cannot_change_decision(monkeypatch):
    from llm import ChatResult
    original = '{"action":"researcher","instruction":"补读结尾"}'
    captured = []
    answers = iter([ChatResult("", original, "<result>"+original, "fake", False),
                    ChatResult("格式", original, "", "fake", True)])
    def chat(role, system, user):
        captured.append((system, user))
        return next(answers)
    monkeypatch.setattr(graph.llm, "chat", chat)
    result, entry = graph._run("orchestrator", "ai_os", "原系统", "很长的材料与历史")
    assert entry["contract_repairs"] == 1 and result == original
    assert "很长的材料" not in captured[1][1]
    assert "不得改变JSON" in captured[1][0]


def test_ai_os_format_repair_rejects_changed_action(monkeypatch):
    from llm import ChatResult
    answers = iter([ChatResult("", '{"action":"researcher"}', "", "fake", False),
                    ChatResult("", '{"action":"human_final"}', "", "fake", True)])
    monkeypatch.setattr(graph.llm, "chat", lambda *args: next(answers))
    with pytest.raises(ValueError, match="未保留原JSON"):
        graph._run("orchestrator", "ai_os", "system", "user")
