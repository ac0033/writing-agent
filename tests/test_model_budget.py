from contextlib import nullcontext
from types import SimpleNamespace

import pytest


def test_failed_model_request_still_counts_and_resume_cannot_reset(monkeypatch):
    import config
    from service.model_budget import budget_observer, model_request, observer_for, BudgetExceeded
    monkeypatch.setattr(config, "AI_OS_MAX_MODEL_CALLS", 1)
    task, saved = {}, []
    token = budget_observer.set(observer_for(task, lambda t: saved.append(dict(t)), nullcontext))
    try:
        with pytest.raises(RuntimeError, match="transport"):
            with model_request("test"):
                raise RuntimeError("transport")
        assert task["model_calls"] == 1
        assert "model_seconds" in task
        with pytest.raises(BudgetExceeded):
            with model_request("test"):
                raise AssertionError("不应再次发出请求")
    finally:
        budget_observer.reset(token)
    assert saved[-1]["model_calls"] == 1


def test_tool_only_api_stream_is_valid():
    import llm
    tc = SimpleNamespace(index=0, id="call1", function=SimpleNamespace(name="search", arguments='{"query":"x"}'))
    stream = [SimpleNamespace(choices=[SimpleNamespace(finish_reason="tool_calls",
        delta=SimpleNamespace(content=None, reasoning_content=None, tool_calls=[tc]))])]
    result = llm._collect_stream("architect", "test", stream, {"content": [], "reasoning": [], "tool_calls": {}})
    assert result.choices[0].message.tool_calls[0].function.name == "search"
    assert result.choices[0].message.content is None


def test_error_message_does_not_save_provider_secret():
    import llm
    error = llm._friendly_error("writer", "deepseek", "test", RuntimeError("sk-private-secret"), False)
    assert "sk-private-secret" not in str(error)
