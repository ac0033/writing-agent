"""v2 闭环回归：真实调用以显式替身替代，不发送用户材料。"""
import json

import pytest

import pipeline_v2 as p


def state(**extra):
    return {"shared_summary": "成本优先", "summary_confirmed": True, "summary_version": 1,
            "article_version": 1, "outline": "结构", "draft": "旧稿", "topic_id": "demo", **extra}


def issue_state(attempts=0, version=1):
    issue = {"id": "reviewer:cost", "source": "reviewer", "text": "成本优先偏离质量要求", "status": "open",
             "revision_attempts": attempts, "last_seen_version": version}
    return state(issue_registry={issue["id"]: issue}, review_verdict="fail", active_issue_ids=[issue["id"]])


def test_summary_merge_replaces_old_position_and_keeps_exploration_separate(monkeypatch):
    monkeypatch.setattr(p.config, "MOCK_LLM", False)
    captured = {}
    def run(role, node, system, user):
        captured.update(json.loads(user))
        return json.dumps({"summary": "质量优先，成本次要", "explicit_update": True,
                           "explorations": ["是否还需要谈速度？"], "preferences": []}), {"node": node}
    monkeypatch.setattr(p.legacy, "_run", run)
    original = state()
    result = p.apply_user_update(original, "改为质量优先，成本次要。是否还需要谈速度？")
    assert result["shared_summary"] == "质量优先，成本次要"
    assert result["summary_version"] == 2 and result["revision_pending"]
    assert captured["current_summary"] == original["shared_summary"] == "成本优先"
    assert result["pending_explorations"] == ["是否还需要谈速度？"]


def test_exploration_does_not_change_confirmed_summary():
    result = p.apply_user_update(state(), "要不要改为质量优先？")
    assert result["shared_summary"] == "成本优先"
    assert result["summary_version"] == 1
    assert not result.get("revision_pending")


def test_preferences_require_user_quote_and_topic_scope(monkeypatch):
    monkeypatch.setattr(p.config, "MOCK_LLM", False)
    result = {"summary": "平实", "explicit_update": True, "preferences": [{"category": "tone", "quote": "语气平实"}]}
    monkeypatch.setattr(p.legacy, "_run", lambda *a: (json.dumps(result), {}))
    delta = p.apply_user_update(state(), "语气平实")
    pref = delta["confirmed_preferences"][0]
    assert pref["source"] == "user" and pref["scope"] == "demo"
    result["preferences"][0]["quote"] = "喜欢夸张"
    with pytest.raises(ValueError, match="用户原话"):
        p.apply_user_update(state(), "语气平实")


def test_two_actual_revisions_then_review_failure_pauses(monkeypatch):
    s = issue_state()
    monkeypatch.setattr(p.legacy, "writer", lambda s: {"draft": s["draft"] + "修"})
    failed = {"review_verdict": "fail", "review_dimensions": {"intent": "fail"},
              "review_comments": 'ISSUE: {"id":"reviewer:cost","text":"仍偏离质量要求"}'}
    monkeypatch.setattr(p.legacy, "reviewer", lambda s: dict(failed))
    for attempt in (1, 2):
        s.update(p.specialist("writer")(s))
        assert s["issue_registry"]["reviewer:cost"]["revision_attempts"] == attempt
        # 第二次修改之后必须先允许审核，不能提前暂停。
        assert p.ai_os(s)["ai_os_next"] != "human_decision"
        s.update(p.specialist("reviewer")(s))
    assert p.ai_os(s)["ai_os_next"] == "human_decision"
    assert len(s["issue_registry"]["reviewer:cost"]["attempts"]) == 2


def test_different_issue_does_not_inherit_other_issue_attempts():
    s = issue_state(1)
    registry = p._sync_issues(s, {"review_comments": 'ISSUE: {"id":"new","text":"新事实问题"}\nRESOLVED: reviewer:cost'}, "reviewer", True)
    assert registry["reviewer:new"]["revision_attempts"] == 0
    assert registry["reviewer:cost"]["status"] == "resolved"


def test_repeated_unchanged_audit_pauses():
    s = issue_state()
    failed = {"review_comments": 'ISSUE: {"id":"reviewer:cost","text":"成本优先偏离质量要求"}'}
    for _ in range(2):
        s["issue_registry"] = p._sync_issues(s, failed, "reviewer", True)
    assert p.ai_os(s)["ai_os_next"] == "human_decision"


def test_independent_review_once_cannot_pass_whole_article(monkeypatch):
    s = issue_state()
    s["review_dispute"] = {"issue_id": "reviewer:cost", "reason": "用户原文支持"}
    monkeypatch.setattr(p.config, "MOCK_LLM", False)
    monkeypatch.setattr(p.legacy, "_run", lambda *a: (json.dumps({"resolved": True, "reason": "原文支持", "evidence": [{"source": "shared_summary", "quote": "成本优先"}]}), {}))
    delta = p.independent_review(s)
    assert delta["issue_registry"]["reviewer:cost"]["status"] == "resolved"
    assert "review_verdict" not in delta and "publication_ready" not in delta
    with pytest.raises(ValueError, match="尚未复核"):
        p.independent_review({**s, **delta})


def test_jev_conflict_rechecks_once_and_requires_supplement(monkeypatch):
    import jev_adapter
    monkeypatch.setattr(jev_adapter, "evaluate_decision", lambda r: {"executable": False})
    s = state(jev_request={"decision_id": "d"}, jev_conflicts={"d": {"reason": "冲突"}})
    with pytest.raises(ValueError, match="补充材料"):
        p.jev(s)
    s["jev_request"]["supplemental_evidence"] = ["新证据"]
    delta = p.jev(s)
    assert delta["jev_conflicts"]["d"]["reevaluations"] == 1
    with pytest.raises(ValueError, match="最多重评一次"):
        p.jev({**s, **delta})


def test_short_sample_produces_options_and_records_only_user_choice(monkeypatch):
    s = state(sample_requested=True)
    s.update(p.sample(s))
    assert set(s["sample_options"]) == {"A", "B"}
    monkeypatch.setattr(p, "interrupt", lambda payload: {"choice": "B"})
    delta = p.human_sample(s)
    assert delta["sample_confirmed"]
    assert delta["confirmed_preferences"][0]["source"] == "user"
    assert delta["confirmed_preferences"][0]["scope"] == "demo"
    assert "短样稿B" in delta["shared_summary"]
    assert not delta["publication_ready"]


def test_runtime_settings_keep_article_preferences():
    s = state(confirmed_preferences=[{"origin": "article_user", "text": "平实"}])
    token = p.runtime_settings_reader.set(lambda: {"jev_settings": {"mode": "shadow"}, "confirmed_preferences": []})
    try:
        delta = p.ai_os(s)
        assert delta["ai_os_next"] == "ai_os"
        assert delta["confirmed_preferences"] == s["confirmed_preferences"]
        assert "jev" in p._decision_actions({**s, **delta})
    finally:
        p.runtime_settings_reader.reset(token)


def test_short_sample_graph_stops_for_real_choice_before_article(monkeypatch):
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.types import Command
    monkeypatch.setattr(p.legacy, "architect", lambda s: {"outline": "o"})
    monkeypatch.setattr(p.legacy, "researcher", lambda s: {"materials": []})
    monkeypatch.setattr(p.legacy, "writer", lambda s: {"draft": "d"})
    monkeypatch.setattr(p.legacy, "reviewer", lambda s: {"review_verdict": "pass", "review_dimensions": {k: "pass" for k in ("intent", "facts", "reading")}})
    monkeypatch.setattr(p.legacy, "stylist", lambda s: {"polished": "p"})
    monkeypatch.setattr(p.legacy, "final_check", lambda s: {"final_check_verdict": "pass", "publication_ready": True, "quality_issues": []})
    g = p.build_graph_v2(InMemorySaver())
    cfg = {"configurable": {"thread_id": "sample-flow"}, "recursion_limit": 100}
    g.invoke({"topic": "t", "user_idea": "质量优先", "sample_requested": True}, cfg)
    assert g.get_state(cfg).next == ("human_summary",)
    g.invoke(Command(resume={"approved": True, "expected_summary_version": 1}), cfg)
    assert g.get_state(cfg).next == ("human_sample",)
    assert not g.get_state(cfg).values.get("draft")
    g.invoke(Command(resume={"choice": "A"}), cfg)
    assert g.get_state(cfg).next == ("human_final",)
    assert g.get_state(cfg).values["confirmed_preferences"][0]["source"] == "user"


def test_final_check_stable_issue_and_explicit_resolution():
    s = state()
    failed = {"quality_issues": ["[ISSUE:claim-1] 缺少原文"], "final_check_comments": "{}"}
    s["issue_registry"] = p._sync_issues(s, failed, "final_check", True)
    assert "final_check:claim-1" in s["issue_registry"]
    failed = {"quality_issues": ["[ISSUE:claim-2] 新问题"], "final_check_comments": '{"resolved_issues":["final_check:claim-1"]}'}
    registry = p._sync_issues(s, failed, "final_check", True)
    assert registry["final_check:claim-1"]["status"] == "resolved"
    assert registry["final_check:claim-2"]["status"] == "open"


def test_issue_cannot_reset_dispute_permission_or_rename_counter():
    s = issue_state(2)
    s["issue_registry"]["reviewer:cost"]["dispute_reviewed"] = True
    comments = 'ISSUE: {"id":"renamed","text":"成本优先偏离质量要求","dispute_reviewed":false,"revision_attempts":0}'
    registry = p._sync_issues(s, {"review_comments": comments}, "reviewer", True)
    assert set(registry) == {"reviewer:cost"}
    assert registry["reviewer:cost"]["revision_attempts"] == 2
    assert registry["reviewer:cost"]["dispute_reviewed"]


def test_omitted_unresolved_issue_still_stops_after_two_revisions():
    s = issue_state(2, version=1)
    s["article_version"] = 3
    s["issue_registry"] = p._sync_issues(s, {"review_comments": 'ISSUE: {"id":"new","text":"换个说法"}'}, "reviewer", True)
    assert p.ai_os(s)["ai_os_next"] == "human_decision"


def test_same_issue_cannot_be_failed_and_resolved_in_one_audit():
    s = issue_state(2)
    comments = 'ISSUE: {"id":"reviewer:cost","text":"还没解决"}\nRESOLVED: reviewer:cost'
    registry = p._sync_issues(s, {"review_comments": comments}, "reviewer", True)
    assert registry["reviewer:cost"]["status"] == "open"


def test_independent_review_cannot_use_its_own_dispute_as_evidence(monkeypatch):
    s = issue_state()
    s["review_dispute"] = {"issue_id": "reviewer:cost", "reason": "凭空新增的统计已经证实"}
    monkeypatch.setattr(p.config, "MOCK_LLM", False)
    monkeypatch.setattr(p.legacy, "_run", lambda *a: (json.dumps({"resolved": True,
        "reason": "支持", "evidence": [{"quote": "凭空新增的统计已经证实"}]}), {}))
    with pytest.raises(ValueError, match="输入材料定位"):
        p.independent_review(s)


def test_multiple_feedback_keeps_prior_exploratory_questions():
    s = state(pending_explorations=["是否需要案例？"])
    delta = p.apply_user_update(s, "要不要讨论价格？")
    assert delta["pending_explorations"] == ["是否需要案例？", "要不要讨论价格？"]


def test_jev_same_question_new_id_cannot_restart_reevaluation(monkeypatch):
    import jev_adapter
    monkeypatch.setattr(jev_adapter, "evaluate_decision", lambda r: pytest.fail("不能再请求"))
    s = state(jev_request={"decision_id": "new-id", "question": "哪个开头", "category": "opening", "supplemental_evidence": ["补充"]},
        jev_conflicts={"old-id": {"question": "哪个开头", "category": "opening", "reevaluations": 1}})
    with pytest.raises(ValueError, match="最多重评一次"):
        p.jev(s)


@pytest.mark.parametrize("category,source,allowed", [
    ("reading", "current_article", True),
    ("facts", "current_article", False),
    ("reading", "shared_summary", False),
])
def test_independent_review_quote_belongs_to_declared_source(monkeypatch, category, source, allowed):
    monkeypatch.setattr(p.config, "MOCK_LLM", False)
    result = {"resolved": True, "reason": "逐字核对", "evidence": [{"source": source, "quote": "当前稿独有段落"}]}
    monkeypatch.setattr(p.legacy, "_run", lambda *a: (json.dumps(result), {"node": "independent_review"}))
    current = issue_state()
    current["draft"] = "当前稿独有段落"
    current["issue_registry"]["reviewer:cost"]["category"] = category
    current["review_dispute"] = {"issue_id": "reviewer:cost"}
    if allowed:
        assert p.independent_review(current)["issue_registry"]["reviewer:cost"]["status"] == "resolved"
    else:
        with pytest.raises(ValueError, match="输入材料定位"):
            p.independent_review(current)


def test_ai_os_receives_actual_style_reads(monkeypatch):
    monkeypatch.setattr(p.config, "MOCK_LLM", False)
    captured = {}
    def run(role, node, system, user):
        captured.update(json.loads(user))
        return json.dumps({"action": "architect", "instruction": "依据已读范文定框架", "reason": "需组织结构"}), {}
    monkeypatch.setattr(p.legacy, "_run", run)
    current = state(outline="", style_references=[{"source": "author/test.md", "text": "已读连续段落", "sha256": "test"}])
    p.ai_os(current)
    assert captured["style_references"] == current["style_references"]


def test_summary_uses_recorded_prompt_file(monkeypatch):
    monkeypatch.setattr(p.config, "MOCK_LLM", False)
    captured = {}
    def run(role, node, system, user):
        captured["files"] = p.legacy._prompt_files.get()
        return "待确认摘要", {}
    monkeypatch.setattr(p.legacy, "_run", run)
    result = p.summary(state(topic="测试"))
    assert any(row["path"].endswith("summary.md") for row in captured["files"])
    assert result["summary_confirmed"] is False


@pytest.mark.parametrize("question", [None, {}, [], "   "])
def test_ai_os_human_question_must_be_real_text(monkeypatch, question):
    monkeypatch.setattr(p.config, "MOCK_LLM", False)
    monkeypatch.setattr(p.legacy, "_run", lambda *args: (json.dumps({"action": "human_decision", "question": question}), {}))
    with pytest.raises(ValueError, match="具体问题"):
        p.ai_os(state(reviewed_version=1))


def test_new_article_cannot_be_disguised_as_final_in_general_question():
    current = state(polished="新润色稿", article_version=4, reviewed_version=3)
    assert "human_decision" not in p._allowed(current)
    assert "reviewer" in p._allowed(current)
    assert "human_final" not in p._allowed(current)
    current["reviewed_version"] = 4
    assert "human_decision" in p._allowed(current)
