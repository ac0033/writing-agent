"""跨审核节点沿用原问题，不以加前缀重置修订预算。"""
from copy import deepcopy

import pytest

import pipeline_v2 as p


KEY = "reviewer:facts-unqualified-model-claims"


def prior_issue():
    return {"id": KEY, "source": "reviewer", "text": "模型能力断言缺少范围限定", "status": "resolved",
            "revision_attempts": 1, "last_seen_version": 4, "resolved_version": 4,
            "attempts": [{"node": "writer", "instruction": "缩小范围", "article_version": 2}],
            "dispute_reviewed": True, "dispute_result": {"resolved": False, "reason": "仍需限定"}}


def test_final_check_reopens_existing_reviewer_id_and_preserves_ownership():
    old = prior_issue()
    original = deepcopy(old)
    state = {"article_version": 4, "issue_registry": {KEY: old}}
    update = {"quality_issues": [f"[ISSUE:{KEY}] 仍存在没有限定范围的断言"], "final_check_comments": "{}"}
    registry = p._sync_issues(state, update, "final_check", True)
    assert set(registry) == {KEY}
    issue = registry[KEY]
    assert issue["source"] == "reviewer" and issue["status"] == "open"
    assert issue["revision_attempts"] == 1 and issue["attempts"] == original["attempts"]
    assert issue["dispute_result"] == original["dispute_result"]
    assert issue["review_sources"] == ["reviewer", "final_check"]
    assert issue["last_reported_by"] == "final_check"
    assert old == original


def test_cross_review_targeted_revision_reaches_original_limit_after_recheck(monkeypatch):
    state = {"shared_summary": "不写无范围断言", "summary_confirmed": True, "outline": "o", "draft": "旧稿",
             "article_version": 4, "issue_registry": {KEY: prior_issue()}}
    fail = {"quality_issues": [f"[ISSUE:{KEY}] 限定仍不充分"], "final_check_comments": "{}"}
    state["issue_registry"] = p._sync_issues(state, fail, "final_check", True)
    state["active_issue_ids"] = [KEY]
    monkeypatch.setattr(p.legacy, "writer", lambda s: {"draft": "新稿加入限定"})
    state.update(p.specialist("writer")(state))
    assert state["article_version"] == 5
    assert state["issue_registry"][KEY]["revision_attempts"] == 2
    assert len(state["issue_registry"][KEY]["attempts"]) == 2
    assert p.ai_os(state)["ai_os_next"] != "human_decision"
    state["issue_registry"] = p._sync_issues(state, fail, "final_check", True)
    assert state["issue_registry"][KEY]["revision_attempts"] == 2
    assert p.ai_os(state)["ai_os_next"] == "human_decision"


def test_known_id_wins_over_same_text_alias_from_other_issue():
    old = prior_issue()
    alternate = "final_check:unrelated"
    registry = {KEY: old, alternate: {"id": alternate, "source": "final_check", "text": "措辞相同", "status": "open", "revision_attempts": 0}}
    result = p._sync_issues({"article_version": 5, "issue_registry": registry},
                           {"quality_issues": [f"[ISSUE:{KEY}] 措辞相同"], "final_check_comments": "{}"}, "final_check", True)
    assert result[KEY]["status"] == "open" and result[KEY]["revision_attempts"] == 1
    assert result[KEY]["text"] == "措辞相同"
    assert result[alternate]["revision_attempts"] == 0


def test_reporting_auditor_can_resolve_cross_node_issue_without_clearing_unseen_ones():
    state = {"article_version": 4, "issue_registry": {KEY: prior_issue(),
             "reviewer:other": {"id": "reviewer:other", "source": "reviewer", "status": "open", "text": "另一阅读问题"}}}
    state["issue_registry"] = p._sync_issues(state, {"quality_issues": [f"[ISSUE:{KEY}] 残留事实问题"]}, "final_check", True)
    result = p._sync_issues(state, {}, "final_check", False)
    assert result[KEY]["status"] == "resolved"
    assert result["reviewer:other"]["status"] == "open"


def test_explicit_resolution_cannot_override_same_call_cross_node_failure():
    state = {"article_version": 4, "issue_registry": {KEY: prior_issue()}}
    update = {"quality_issues": [f"[ISSUE:{KEY}] 残留问题"],
              "final_check_comments": '{"resolved_issues":["' + KEY + '"]}'}
    result = p._sync_issues(state, update, "final_check", True)
    assert result[KEY]["status"] == "open"


def test_fix_does_not_silently_rewrite_historical_duplicate_ids():
    # 已污染的检查点需要独立维护记录，正常审核不能猜测两条记录的合并次数。
    wrong = "final_check:" + KEY
    state = {"article_version": 5, "issue_registry": {KEY: prior_issue(),
             wrong: {"id": wrong, "source": "final_check", "status": "open", "text": "历史重复", "revision_attempts": 1}}}
    result = p._sync_issues(state, {"quality_issues": [f"[ISSUE:{KEY}] 残留问题"]}, "final_check", True)
    assert result[KEY]["revision_attempts"] == 1
    assert result[wrong]["revision_attempts"] == 1
    assert len(result) == 2


def test_new_issue_with_foreign_prefix_is_registered_under_reporting_node():
    # 真实任务里 reviewer 报出过 final_check:codex-dialogue-project-attribution，被登记成 reviewer:final_check:...。
    result = p._sync_issues({"article_version": 5, "issue_registry": {}},
        {"review_comments": 'ISSUE: {"id":"final_check:codex-dialogue-project-attribution","text":"归属无依据"}'}, "reviewer", True)
    assert set(result) == {"reviewer:codex-dialogue-project-attribution"}
    assert result["reviewer:codex-dialogue-project-attribution"]["source"] == "reviewer"


def test_research_gaps_and_status_lines_are_not_registered_as_article_issues():
    gap = "工具条件：本节点只有 read_source_window，不能读取本地素材库。"
    update = {"quality_issues": ["[ISSUE:overclaim] 第一节称“最容易被跳过”无依据", "论断未获得支持：现在很多开发都叫 vibe coding。",
                                 gap, "润色后事实与表达复核未通过", "初稿审核尚未通过"], "final_check_comments": "{}"}
    result = p._sync_issues({"article_version": 4, "issue_registry": {}, "research_gaps": [gap]}, update, "final_check", True)
    texts = sorted(issue["text"] for issue in result.values())
    assert texts == ["第一节称“最容易被跳过”无依据", "论断未获得支持：现在很多开发都叫 vibe coding。"]
    # 发布阻塞原因本身不被改写
    assert gap in update["quality_issues"] and "润色后事实与表达复核未通过" in update["quality_issues"]


def test_failure_with_only_status_lines_still_records_one_issue():
    result = p._sync_issues({"article_version": 4, "issue_registry": {}},
        {"quality_issues": ["润色后事实与表达复核未通过"], "final_check_comments": "核验未通过，但未列出具体问题"}, "final_check", True)
    assert len(result) == 1 and next(iter(result.values()))["status"] == "open"


def test_polish_is_not_blocked_by_stale_final_check_entries_but_current_issues_still_require_ids(monkeypatch):
    # 真实任务：v7 审核全通过后 AI OS 安排润色，却因 v4 成稿核验遗留的未复核条目被拦。
    stale = {"id": "final_check:old", "source": "final_check", "status": "open", "text": "旧核验条目", "last_seen_version": 4}
    base = {"shared_summary": "s", "summary_confirmed": True, "outline": "o", "draft": "稿", "article_version": 7,
            "reviewed_version": 7, "review_verdict": "pass", "review_dimensions": {"intent": "pass", "facts": "pass", "reading": "pass"},
            "issue_registry": {"final_check:old": stale}}
    monkeypatch.setattr(p, "_mock_action", lambda s: "stylist")
    monkeypatch.setattr(p.config, "MOCK_LLM", True)
    assert p.ai_os(dict(base))["ai_os_next"] == "stylist"
    monkeypatch.setattr(p.config, "MOCK_LLM", False)
    monkeypatch.setattr(p.legacy, "_run", lambda *a, **k: ('{"action":"stylist","instruction":"润色","reason":"r","issue_ids":[],"question":""}', {"node": "ai_os", "model": "m", "thinking": ""}))
    monkeypatch.setattr(p.legacy, "_load_prompt", lambda name: "")
    assert p.ai_os(dict(base))["ai_os_next"] == "stylist"
    current = {**base, "issue_registry": {"reviewer:now": {"id": "reviewer:now", "source": "reviewer", "status": "open", "text": "当前问题", "last_seen_version": 7}}}
    with pytest.raises(ValueError, match="issue_ids：reviewer:now"):
        p.ai_os(dict(current))


def test_no_plain_question_between_review_pass_and_final_check():
    base = {"draft": "稿", "outline": "o", "article_version": 7, "reviewed_version": 7, "review_verdict": "pass",
            "review_dimensions": {"intent": "pass", "facts": "pass", "reading": "pass"}, "checked_version": -1}
    assert "human_decision" not in p._allowed(base) and "stylist" in p._allowed(base)
    assert "human_decision" not in p._allowed({**base, "polished": "润"}) and "final_check" in p._allowed({**base, "polished": "润"})
    assert "human_decision" in p._allowed({**base, "review_verdict": "fail"})      # 审核未通过时仍可提问
    assert "human_decision" in p._allowed({**base, "checked_version": 7})          # 核验已跑过


def test_reviewer_not_offered_again_for_an_already_reviewed_version():
    # 真实任务：AI OS 让 reviewer 重审已通过的 v7 并称其“等效于成稿核验”，白耗一次调度。
    base = {"draft": "稿", "outline": "o", "article_version": 7, "reviewed_version": 7, "review_verdict": "pass",
            "review_dimensions": {"intent": "pass", "facts": "pass", "reading": "pass"}, "checked_version": -1}
    assert "reviewer" not in p._allowed(base) and "stylist" in p._allowed(base)
    assert "reviewer" in p._allowed({**base, "reviewed_version": 6})


def test_after_review_pass_only_forward_actions_are_offered():
    base = {"draft": "稿", "outline": "o", "article_version": 7, "reviewed_version": 7, "review_verdict": "pass",
            "review_dimensions": {"intent": "pass", "facts": "pass", "reading": "pass"}, "checked_version": -1,
            "issue_registry": {"final_check:old": {"status": "open", "source": "final_check", "last_seen_version": 4}}}
    assert p._allowed(base) == ["stylist"]
    assert p._allowed({**base, "polished": "润"}) == ["final_check"]
    assert "architect" in p._allowed({**base, "review_verdict": "fail"})
    # 成稿核验失败后要能定向修订
    assert "writer" in p._allowed({**base, "polished": "润", "checked_version": 7, "final_check_verdict": "fail"})


def test_final_check_is_not_repeated_on_an_already_checked_version():
    base = {"draft": "稿", "polished": "润", "outline": "o", "article_version": 8, "reviewed_version": 8, "review_verdict": "pass",
            "review_dimensions": {"intent": "pass", "facts": "pass", "reading": "pass"}, "checked_version": 8,
            "final_check_verdict": "pass", "publication_ready": False, "quality_issues": ["旧缺口"]}
    assert "final_check" in p._allowed(base)          # 核验通过但程序校验未过：允许重新核验，由重复次数规则兜底
    assert "final_check" not in p._allowed({**base, "publication_ready": True, "quality_issues": []})
    assert "final_check" in p._allowed({**base, "checked_version": 7})
