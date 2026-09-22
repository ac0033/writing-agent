"""本地设置与旁路评估测试；不产生真实用户选择或模型请求。"""
from copy import deepcopy

import pytest

from service.jev_settings import (apply_settings, content_fingerprint, default_settings,
    evaluation_report, load_settings, prediction_input, runtime_state, save_settings)


def apply(settings, action, **fields):
    return apply_settings(settings, dict(action=action, user_event_id="test-user-event", **fields))


def test_preferences_scope_correction_and_persistence(tmp_path):
    empty = default_settings()
    first = apply(empty, "preference", id="p1", text="用案例开头", source_ref="chat:1", scope="topic-a")
    assert not empty["preferences"]
    second = apply(first, "correct_preference", id="p2", text="先交代问题", source_ref="chat:2",
                   scope="topic-a", supersedes="p1")
    assert runtime_state(second, "topic-a")["confirmed_preferences"][0]["id"] == "p2"
    assert not runtime_state(second, "topic-b")["confirmed_preferences"]
    assert first["preferences"][0]["active"]
    path = tmp_path / "settings.json"
    save_settings(path, second)
    assert load_settings(path) == second
    assert load_settings(tmp_path / "missing.json")["mode"] == "off"
    second["mode"] = "enabled"
    save_settings(path, second)
    with pytest.raises(ValueError, match="审计"):
        load_settings(path)


@pytest.mark.parametrize("operation", [
    {"action": "mode", "mode": "enabled"},
    {"action": "category", "category": "publish", "policy": {}},
    {"action": "category", "category": "opening", "policy": {"enabled": True}},
    {"action": "authorize", "provider": "openai", "granted": True},
    {"action": "preference", "id": "p1", "text": "推测", "scope": "global"},
])
def test_incomplete_authorization_rejected(operation):
    with pytest.raises(ValueError):
        apply_settings(default_settings(), dict(operation, user_event_id="event"))


def test_category_requires_evaluation_references_and_thresholds():
    policy = dict(enabled=True, shadow_allowed=True, evaluation_passed=True, user_approved=True,
                  evaluation_report_ref="report.md", evaluation_plan_ref="plan.md",
                  min_probability=.95, min_confidence=.9)
    settings = apply(default_settings(), "category", category="opening", policy=policy)
    assert apply(settings, "mode", mode="enabled")["mode"] == "enabled"
    for field in ("evaluation_report_ref", "evaluation_plan_ref", "min_probability", "min_confidence"):
        bad = deepcopy(policy)
        del bad[field]
        with pytest.raises(ValueError):
            apply(default_settings(), "category", category="opening", policy=bad)
    corrected = apply(settings, "preference", id="p1", text="新版偏好", source_ref="chat:2", scope="global")
    assert not corrected["categories"]["opening"]["evaluation_passed"]


def sample():
    row = dict(decision_id="v1", category="opening", question="选开头", shared_summary="案例说明任务选择",
        preferences=[dict(id="p1", text="具体", source="user")], candidates=[dict(id="a"), dict(id="b")],
        summary_revision=1, article_revision=1, user_choice="a", user_event_id="answer:1",
        label_source="user", label_sequence=10)
    fingerprint = content_fingerprint(prediction_input(row))
    row["predictions"] = {model: dict(choice=choice, sequence=5, input_fingerprint=fingerprint)
        for model, choice in (("jev", "a"), ("ai_os", "b"), ("rules", "__ask_user__"))}
    return row


def test_no_answer_in_prediction_payload():
    row = sample()
    row["later_correction"] = "选b"
    payload = prediction_input(row)
    assert not {"user_choice", "user_event_id", "predictions", "later_correction"} & payload.keys()
    payload["preferences"][0]["text"] = "mutated"
    assert row["preferences"][0]["text"] == "具体"


def test_three_baselines_and_revocation():
    report = evaluation_report([], [sample()], [dict(decision_id="v1")])
    group = report["categories"]["opening"]
    assert group["jev"]["agreement_rate"] == 1
    assert group["jev"]["revocation_rate"] == 1
    assert group["ai_os"]["agreement_rate"] == 0
    assert group["rules"]["unnecessary_ask_rate"] == 1
    assert not report["auto_enable"] and not report["personal_validation_passed"]


def test_no_implicit_labels():
    row = sample()
    row["label_source"] = "ai_os"
    assert evaluation_report([], [row])["categories"] == {}


@pytest.mark.parametrize("bad_case", ["overlap", "duplicate", "late", "different_input", "missing_baseline"])
def test_evaluation_rejects_contaminated_or_incomplete_records(bad_case):
    row = sample()
    discovery, validation = [], [row]
    if bad_case == "overlap":
        discovery = [dict(decision_id="v1")]
    elif bad_case == "duplicate":
        validation.append(row)
    elif bad_case == "late":
        row["predictions"]["jev"]["sequence"] = row["label_sequence"]
    elif bad_case == "different_input":
        row["preferences"][0]["text"] = "看过答案后的偏好"
    else:
        del row["predictions"]["ai_os"]
    with pytest.raises(ValueError):
        evaluation_report(discovery, validation)


def test_external_authorization_and_revoke():
    row = sample()
    settings = apply(default_settings(), "authorize", provider="typesafe", granted=True,
        decision_id="v1", content_fingerprint=content_fingerprint(row), purpose="旁路评估")
    assert runtime_state(settings, "topic", "v1")["jev_external_authorization"]["granted"]
    settings = apply(settings, "revoke_authorization", decision_id="v1")
    assert not runtime_state(settings, "topic", "v1")["jev_external_authorization"]


def test_settings_off_overrides_global_enabled(monkeypatch):
    import config
    from jev_adapter import evaluate_decision
    monkeypatch.setattr(config, "JEV_MODE", "enabled")
    assert evaluate_decision(dict(settings=default_settings()))["reason"] == "disabled"


def test_changed_content_invalidates_external_authorization(monkeypatch):
    import jev_adapter
    request = prediction_input(sample())
    request.update(user_reserved=False, explicit_user_choice=False,
        settings=dict(mode="shadow", categories={"opening": dict(shadow_allowed=True)}))
    for candidate in request["candidates"]:
        candidate.update(text=candidate["id"], eligible=True)
    request["external_authorization"] = dict(provider="typesafe", granted=True,
        decision_id=request["decision_id"], content_fingerprint=content_fingerprint(request))
    request["article_revision"] += 1
    monkeypatch.setattr(jev_adapter, "_post", lambda _: pytest.fail("变更材料不得发送"))
    assert jev_adapter.evaluate_decision(request)["reason"] == "external_content_changed"


def test_user_revocation_disables_delegation():
    policy = dict(enabled=True, shadow_allowed=True, evaluation_passed=True, user_approved=True,
        evaluation_report_ref="report.md", evaluation_plan_ref="plan.md", min_probability=.95, min_confidence=.9)
    settings = apply(default_settings(), "category", category="opening", policy=policy)
    settings = apply(settings, "mode", mode="enabled")
    settings = apply(settings, "revoke_decision", decision_id="d1", reason="不符合原意")
    assert settings["mode"] == "off"
    assert not settings["categories"]["opening"]["evaluation_passed"]


def test_prediction_collection_then_real_label_and_report(tmp_path):
    import json
    from service.jev_settings import collect_prediction, record_user_choice
    from scripts.jev_evaluate import generate_report
    path = tmp_path / "validation.json"
    inputs = prediction_input(sample())
    for model, choice in (("jev", "a"), ("ai_os", "b"), ("rules", "__ask_user__")):
        row = collect_prediction(path, inputs, model, choice, "synthetic-test-output")
    labelled = record_user_choice(path, "v1", "a", user_event_id="synthetic-user-event",
        source_ref="synthetic-test-chat", user_text="测试模拟回复：选择a")
    assert all(p["sequence"] < labelled["label_sequence"] for p in labelled["predictions"].values())
    discovery = tmp_path / "discovery.json"
    discovery.write_text("[]", encoding="utf-8")
    report = generate_report(discovery, path, tmp_path / "report")
    assert report["categories"]["opening"]["jev"]["agreement_rate"] == 1
    assert (tmp_path / "report" / "report.md").exists()
    assert not report["auto_enable"]
    with pytest.raises(FileExistsError):
        generate_report(discovery, path, tmp_path / "report")
    with pytest.raises(ValueError, match="固化"):
        collect_prediction(path, inputs, "jev", "b", "different-output")
    with pytest.raises(ValueError, match="固化"):
        record_user_choice(path, "v1", "b", user_event_id="second-event", source_ref="test", user_text="改选b")


def test_collection_rejects_leakage_early_label_and_duplicate_inputs(tmp_path):
    from service.jev_settings import collect_prediction, record_user_choice, validate_sample_split
    path = tmp_path / "records.json"
    inputs = prediction_input(sample())
    with pytest.raises(ValueError, match="答案"):
        collect_prediction(path, dict(inputs, user_choice="a"), "jev", "a", "test")
    collect_prediction(path, inputs, "jev", "a", "test")
    with pytest.raises(ValueError, match="三种预测"):
        record_user_choice(path, "v1", "a", user_event_id="event", source_ref="test", user_text="选a")
    with pytest.raises(ValueError, match="重复输入"):
        validate_sample_split([inputs], [dict(inputs, decision_id="renamed")])
    changed = dict(inputs, decision_id="new", question="不同问题", group_id="same-group")
    with pytest.raises(ValueError, match="同组问题"):
        validate_sample_split([dict(inputs, group_id="same-group")], [changed])


def test_evaluation_writer_lock_does_not_overwrite(tmp_path):
    from service.jev_settings import collect_prediction
    path = tmp_path / "records.json"
    path.with_name("records.json.lock").write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="正在写入"):
        collect_prediction(path, prediction_input(sample()), "jev", "a", "test")
    assert not path.exists()
