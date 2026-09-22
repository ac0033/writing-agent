"""验证代决边界；所有预测通过替身返回，真实网络被阻断。"""
from copy import deepcopy

import pytest
import requests

import config
import jev_adapter as jev


@pytest.fixture
def request_data(monkeypatch):
    for key, value in {
        "JEV_MODE": "enabled", "JEV_API_KEY": "test-only",
        "JEV_MODEL": "jev-1.13.0", "JEV_ENDPOINT": "https://api.typesafe.ai/v1/systemone",
        "JEV_TIMEOUT_S": 10,
        "JEV_CATEGORY_POLICIES": {"opening": {"shadow_allowed": True, "enabled": True,
            "evaluation_passed": True, "user_approved": True,
            "min_probability": .9, "min_confidence": .8}},
    }.items():
        monkeypatch.setattr(config, key, value, raising=False)
    def forbidden(*args, **kwargs):
        raise AssertionError("测试不得访问网络")
    monkeypatch.setattr(requests, "post", forbidden)
    return {"decision_id": "d1", "category": "opening", "question": "选哪个开头？",
            "shared_summary": "按任务选择模型", "summary_revision": 2, "article_revision": 3,
            "preferences": [{"id": "u1", "text": "喜欢具体案例", "source": "user"}],
            "candidates": [{"id": "a", "text": "案例进入", "eligible": True},
                           {"id": "b", "text": "问题进入", "eligible": True}],
            "user_reserved": False, "explicit_user_choice": False,
            "external_authorization": {"provider": "typesafe", "decision_id": "d1", "granted": True}}


def answer(choice="a", probability=.95, confidence=.9):
    probs = dict.fromkeys(["a", "b", *jev.RESERVED], 0.0)
    probs[choice] = probability
    probs["b" if choice != "b" else "a"] = 1 - probability
    return {"model": "jev-1.13.0", "answers": {"decision": {
        "type": "choice", "choice": choice, "confidence": confidence, "probabilities": probs}}}


def test_authorized_choice_and_record(request_data, monkeypatch):
    calls = []
    monkeypatch.setattr(jev, "_post", lambda body: calls.append(body) or answer())
    result = jev.evaluate_decision(request_data)
    assert result["executable"] and result["candidate_id"] == "a"
    assert result["summary_revision"] == 2 and result["article_revision"] == 3
    assert result["is_user_preference"] is False
    assert result["evidence_origin"] == "input_user_records"
    assert calls[0]["questions"]["decision"]["type"] == "choice"


@pytest.mark.parametrize("field,value", [
    ("category", "publish"), ("user_reserved", True), ("explicit_user_choice", True),
    ("external_authorization", {}), ("preferences", []),
    ("preferences", [{"id": "j1", "text": "代决偏好", "source": "jev"}]),
    ("candidates", [{"id": "a", "text": "案例", "eligible": False}]),
    ("article_revision", None),
])
def test_invalid_or_reserved_never_calls(request_data, monkeypatch, field, value):
    monkeypatch.setattr(jev, "_post", lambda _: pytest.fail("不应发送"))
    request_data[field] = value
    assert not jev.evaluate_decision(request_data)["executable"]


def test_off_and_shadow(request_data, monkeypatch):
    monkeypatch.setattr(config, "JEV_MODE", "off")
    assert jev.evaluate_decision(request_data)["reason"] == "disabled"
    monkeypatch.setattr(config, "JEV_MODE", "shadow")
    monkeypatch.setattr(jev, "_post", lambda _: answer())
    result = jev.evaluate_decision(request_data)
    assert result["prediction"] == "a" and result["origin"] == "jev_shadow"
    assert result["action"] == "none" and not result["executable"]


@pytest.mark.parametrize("key", ["enabled", "user_approved", "evaluation_passed", "shadow_allowed"])
def test_category_permission(request_data, monkeypatch, key):
    policy = deepcopy(config.JEV_CATEGORY_POLICIES)
    policy["opening"][key] = False
    monkeypatch.setattr(config, "JEV_CATEGORY_POLICIES", policy)
    monkeypatch.setattr(jev, "_post", lambda _: pytest.fail("不应发送"))
    assert not jev.evaluate_decision(request_data)["executable"]


@pytest.mark.parametrize("response", [answer(probability=.7), answer(confidence=.2),
    answer(choice="__ask_user__"), answer(choice="__need_analysis__"),
    {"answers": {}}, answer(probability=float("nan"))])
def test_uncertain_invalid_or_abstained(request_data, monkeypatch, response):
    monkeypatch.setattr(jev, "_post", lambda _: response)
    assert not jev.evaluate_decision(request_data)["executable"]


def test_timeout_does_not_leak_exception(request_data, monkeypatch):
    def fail(_):
        raise requests.Timeout("secret document")
    monkeypatch.setattr(jev, "_post", fail)
    result = jev.evaluate_decision(request_data)
    assert result["status"] == "unavailable" and "secret" not in str(result)


def test_mock_blocks_real_network(request_data):
    assert config.MOCK_LLM
    assert jev.evaluate_decision(request_data)["status"] == "unavailable"


def test_shadow_comparison_deduplicates_real_labels(request_data, monkeypatch):
    monkeypatch.setattr(config, "JEV_MODE", "shadow")
    monkeypatch.setattr(jev, "_post", lambda _: answer())
    prediction = jev.evaluate_decision(request_data)
    row = {"prediction": prediction, "user_choice": "a", "user_decision_id": "u2"}
    results = jev.compare_shadow_records([row, row, {"prediction": prediction}])
    assert results["categories"]["opening"]["labelled"] == 1
    assert results["categories"]["opening"]["agreement_rate"] == 1
    assert results["auto_enable"] is False
