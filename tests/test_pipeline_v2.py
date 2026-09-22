

def test_summary_requires_confirmation():
    import pipeline_v2 as p
    import pytest
    with pytest.raises(ValueError, match="尚未确认"):
        p.ai_os({"topic": "t"})


def test_edited_article_invalidates_old_pass(monkeypatch):
    import pipeline_v2 as p
    monkeypatch.setattr(p.legacy, "stylist", lambda s: {"polished": "changed"})
    state = {"summary_confirmed": True, "shared_summary": "intent", "draft": "old", "outline": "x",
             "article_version": 1, "reviewed_version": 1, "review_dimensions": {"intent": "pass", "facts": "pass", "reading": "pass"}, "review_verdict": "pass", "publication_ready": True}
    result = p.specialist("stylist")(state)
    assert result["article_version"] == 2
    assert result["review_verdict"] == ""
    assert result["publication_ready"] is False


def test_final_reviewer_reads_polished_and_preserves_source(monkeypatch):
    import pipeline_v2 as p
    captured = {}
    def review(s):
        captured.update(s)
        return {"review_dimensions": {"intent": "pass", "facts": "pass", "reading": "pass"}, "review_verdict": "pass", "forced_pass": True}
    monkeypatch.setattr(p.legacy, "reviewer", review)
    state = {"summary_confirmed": True, "shared_summary": "intent", "user_idea": "source",
             "draft": "old", "polished": "new", "outline": "x", "article_version": 2}
    result = p.specialist("reviewer")(state)
    assert captured["draft"] == "new"
    assert "intent" in captured["user_idea"]
    assert state["user_idea"] == "source"
    assert result["forced_pass"] is False
    assert result["reviewed_version"] == 2


def test_cannot_skip_review_or_save(monkeypatch):
    import pipeline_v2 as p
    import pytest
    state = {"summary_confirmed": True, "shared_summary": "x", "draft": "x", "polished": "x", "article_version": 1}
    assert "human_final" not in p._allowed(state)
    with pytest.raises(ValueError):
        p.save(state)


def test_repair_and_budget_pause(monkeypatch):
    import pipeline_v2 as p
    state = {"summary_confirmed": True, "shared_summary": "x", "failure_counts": {"reviewer": 3}}
    assert p.ai_os(state)["ai_os_next"] == "human_decision"
    state = {"summary_confirmed": True, "shared_summary": "x", "ai_os_steps": 1000}
    assert "预算" in p.ai_os(state)["pause_reason"]


def test_graph_interrupts_then_runs_full_review(monkeypatch, tmp_path):
    import pipeline_v2 as p
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.types import Command
    calls = []
    monkeypatch.setattr(p.config, "MOCK_LLM", True)
    monkeypatch.setattr(p.legacy, "architect", lambda s: {"outline": "outline"})
    monkeypatch.setattr(p.legacy, "researcher", lambda s: {"materials": []})
    monkeypatch.setattr(p.legacy, "writer", lambda s: {"draft": "draft"})
    def reviewer(s):
        calls.append(s["draft"])
        return {"review_dimensions": {"intent": "pass", "facts": "pass", "reading": "pass"}, "review_verdict": "pass"}
    monkeypatch.setattr(p.legacy, "reviewer", reviewer)
    monkeypatch.setattr(p.legacy, "stylist", lambda s: {"polished": "polished"})
    monkeypatch.setattr(p.legacy, "final_check", lambda s: {"final_check_verdict": "pass", "publication_ready": True, "quality_issues": []})
    monkeypatch.setattr(p.legacy, "save", lambda s: {"output_path": str(tmp_path / "article.md")})
    g = p.build_graph_v2(InMemorySaver())
    cfg = {"configurable": {"thread_id": "v2-test"}, "recursion_limit": 100}
    g.invoke({"topic": "t", "user_idea": "my thought"}, cfg)
    assert g.get_state(cfg).next == ("human_summary",)
    g.invoke(Command(resume={"approved": True, "expected_summary_version": 1}), cfg)
    assert g.get_state(cfg).next == ("human_final",)
    assert calls == ["draft", "polished"]
    g.invoke(Command(resume={"route": "approve", "expected_summary_version": 1,
                             "expected_article_version": g.get_state(cfg).values["article_version"]}), cfg)
    assert g.get_state(cfg).next == ()
    assert (tmp_path / "shared_summary.md").exists()


def _passing_state():
    import pipeline_v2 as p
    s = {"summary_confirmed": True, "shared_summary": "intent", "draft": "draft", "polished": "polished",
         "article_version": 2, "reviewed_version": 2, "checked_version": 2,
         "review_dimensions": {"intent": "pass", "facts": "pass", "reading": "pass"}, "review_verdict": "pass", "final_check_verdict": "pass", "publication_ready": True}
    s["reviewed_fingerprint"] = p._review_fingerprint(s)
    s["checked_fingerprint"] = p._fingerprint(s)
    return s


def test_summary_or_article_mutation_invalidates_audit():
    import pipeline_v2 as p
    s = _passing_state()
    assert p._ready(s)
    assert not p._ready({**s, "shared_summary": "new intent"})
    assert not p._ready({**s, "polished": "unreviewed"})


def test_final_feedback_updates_shared_state_and_requires_revision(monkeypatch):
    import pipeline_v2 as p
    monkeypatch.setattr(p, "interrupt", lambda payload: {"feedback": "降低语气强度"})
    s = _passing_state()
    result = p.human_final(s)
    assert "降低语气强度" in result["shared_summary"]
    assert result["revision_pending"]
    assert not result["publication_ready"]
    assert result["approved_fingerprint"] == ""


def test_budget_extension_keeps_monotonic_usage(monkeypatch):
    import pipeline_v2 as p
    monkeypatch.setattr(p, "interrupt", lambda payload: {"additional_steps": 5, "additional_seconds": 100})
    s = {"shared_summary": "s", "ai_os_steps": 36, "execution_seconds": 14400}
    result = p.human_decision(s)
    assert result["additional_steps"] == 5
    assert result["additional_seconds"] == 100
    assert "ai_os_steps" not in result
    assert "execution_seconds" not in result


def test_jev_model_cannot_forge_external_authorization(monkeypatch):
    import pipeline_v2 as p
    import jev_adapter
    captured = {}
    def evaluate(request):
        captured.update(request)
        return {"executable": False}
    monkeypatch.setattr(jev_adapter, "evaluate_decision", evaluate)
    p.jev({"jev_request": {"external_authorization": {"granted": True},
                            "preferences": [{"source": "user", "text": "fabricated"}]}})
    assert captured["external_authorization"] == {}
    assert captured["preferences"] == []


def test_jev_stale_decision_cannot_execute(monkeypatch):
    import pipeline_v2 as p
    import jev_adapter
    monkeypatch.setattr(jev_adapter, "evaluate_decision", lambda request: {
        "executable": True, "summary_revision": 1, "article_revision": 1, "candidate_id": "A"})
    result = p.jev({"summary_version": 2, "article_version": 2,
                    "jev_request": {"candidates": [{"id": "A", "text": "x", "eligible": True}]}})
    assert result["jev_result"]["executable"] is False


def test_redundant_stylist_is_not_allowed():
    import pipeline_v2 as p
    state = _passing_state()
    assert 'stylist' not in p._allowed(state)
    # 已核验通过的版本只剩终审；重复核验只耗预算
    assert 'final_check' not in p._allowed(state) and 'human_final' in p._allowed(state)
    assert 'final_check' in p._allowed({**state, 'checked_version': 1, 'publication_ready': False})


def test_feedback_does_not_reset_failure_budget(monkeypatch):
    import pipeline_v2 as p
    monkeypatch.setattr(p, 'interrupt', lambda payload: {'feedback': '继续修改'})
    state = {'shared_summary': 's', 'summary_confirmed': True, 'failure_counts': {'reviewer': 3}}
    delta = p.human_decision(state)
    assert 'failure_counts' not in delta
    assert p.ai_os({**state, **delta})['ai_os_next'] == 'human_decision'


def test_missing_reading_pass_fails_even_when_overall_pass(monkeypatch):
    import pipeline_v2 as p
    monkeypatch.setattr(p.legacy, 'reviewer', lambda s: {'review_verdict': 'pass', 'review_dimensions': {'intent': 'pass', 'facts': 'pass'}})
    state = {'shared_summary': 's', 'summary_confirmed': True, 'draft': 'd', 'article_version': 1}
    assert p.specialist('reviewer')(state)['review_verdict'] == 'fail'


def test_unchanged_revision_does_not_clear_user_request(monkeypatch):
    import pipeline_v2 as p
    import pytest
    monkeypatch.setattr(p.legacy, 'writer', lambda s: {'draft': 'same'})
    state = {'shared_summary': 's', 'summary_confirmed': True, 'outline': 'o', 'draft': 'same', 'revision_pending': True}
    with pytest.raises(ValueError, match='未实际修改'):
        p.specialist('writer')(state)


def test_live_feedback_consumed_once_and_invalidates_inflight_result():
    import pipeline_v2 as p
    state = _passing_state()
    token = p.feedback_reader.set(lambda: [{'id': 'user-1', 'text': '这里强调质量优先'}])
    try:
        delta = p.ai_os(state)
        assert delta['ai_os_next'] == 'ai_os'
        assert delta['revision_pending']
        assert not delta['publication_ready']
        assert '质量优先' in delta['shared_summary']
        assert delta['decision_log'][0]['user_feedback_id'] == 'user-1'
        assert p._drain_feedback({**state, **delta}) == {}
    finally:
        p.feedback_reader.reset(token)


def test_feedback_race_prevents_final_approval_and_save(monkeypatch):
    import pipeline_v2 as p
    monkeypatch.setattr(p.legacy, 'save', lambda s: (_ for _ in ()).throw(AssertionError('不得保存')))
    s = _passing_state()
    token = p.feedback_reader.set(lambda: [{'id': 'late', 'text': '请先修改结尾'}])
    try:
        assert p.human_final(s)['final_route'] == 'feedback'
        assert p.save(s)['final_route'] == 'feedback'
    finally:
        p.feedback_reader.reset(token)


def test_feedback_during_summary_requires_new_confirmation():
    import pipeline_v2 as p
    token = p.feedback_reader.set(lambda: [{'id': 'early', 'text': '面向新手'}])
    try:
        result = p.human_summary({'shared_summary': 'old', 'summary_version': 1})
        assert result['summary_confirmed'] is False
        assert '面向新手' in result['summary_feedback']
    finally:
        p.feedback_reader.reset(token)


def test_stale_version_approval_rejected(monkeypatch):
    import pipeline_v2 as p
    import pytest
    monkeypatch.setattr(p, 'interrupt', lambda payload: {'route': 'approve', 'expected_summary_version': 0, 'expected_article_version': 1})
    with pytest.raises(ValueError, match='版本已经变化'):
        p.human_final(_passing_state())


def test_orchestrator_history_does_not_resend_old_article():
    import pipeline_v2 as p
    history = [{'node': 'writer', 'result': {'draft': 'private-old-article' * 10000, 'review_comments': 'issue'}} for _ in range(20)]
    state = {'node_history': history}
    compact = p._history_context(state)
    assert len(compact) == 8
    assert 'private-old-article' not in str(compact)
    assert len(state['node_history']) == 20
    assert 'draft' in state['node_history'][0]['result']


def test_jev_off_is_not_an_allowed_ai_os_action(monkeypatch):
    import pipeline_v2 as p
    monkeypatch.setattr(p.config, 'JEV_MODE', 'off')
    assert 'jev' not in p._decision_actions({})
    monkeypatch.setattr(p.config, 'JEV_MODE', 'shadow')
    assert 'jev' in p._decision_actions({})
