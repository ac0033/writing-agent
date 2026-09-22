"""CLI 短样稿、人工确认和可续跑预算；只用 mock 节点，不调用真实模型。"""
from types import SimpleNamespace
import json

import pytest

import main
from ai_os_connection import ConnectionSettings, current_connection
from service.model_budget import BudgetExceeded, budget_observer, model_request


@pytest.fixture(autouse=True)
def isolated_cli(monkeypatch, tmp_path):
    monkeypatch.setattr(main.config, "SESSIONS_FILE", tmp_path / "sessions.json")
    monkeypatch.setattr(main.config, "CHECKPOINT_DB", tmp_path / "checkpoints.sqlite")
    monkeypatch.setattr(main.config, "MEMORY_ENABLED", False)
    monkeypatch.setattr(main.config, "MOCK_LLM", True)
    monkeypatch.setattr(main.config, "BLOG_REPO_PATH", "")


@pytest.mark.parametrize("answer,choice,feedback", [("A", "A", ""), ("b", "B", ""), ("想更简洁", None, "想更简洁")])
def test_sample_selection_or_feedback(monkeypatch, answer, choice, feedback):
    monkeypatch.setattr("builtins.input", lambda *a: answer)
    decision = main.handle_interrupt({"kind": "sample", "summary_version": 4, "options": {"A": "样稿一", "B": "样稿二"}})
    assert decision == {"choice": choice, "feedback": feedback, "expected_summary_version": 4}
    assert "approved" not in decision and "route" not in decision


def test_sample_empty_is_not_confirmation(monkeypatch):
    answers = iter(["", "B"])
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))
    assert main.handle_interrupt({"kind": "sample", "summary_version": 1,
        "options": {"A": "one", "B": "two"}})["choice"] == "B"


@pytest.mark.parametrize("answer", ["", "y", "通过", "A"])
def test_v2_summary_and_final_require_explicit_confirmation(monkeypatch, answer):
    monkeypatch.setattr("builtins.input", lambda *a: answer)
    assert not main.handle_interrupt({"kind": "summary", "summary": "intent", "summary_version": 1})["approved"]
    assert main.handle_interrupt({"kind": "final", "pipeline_version": "v2", "polished": "article",
        "summary_version": 1, "article_version": 2})["route"] != "approve"


def test_failed_request_budget_survives_new_runtime(monkeypatch):
    monkeypatch.setattr(main.config, "AI_OS_MAX_MODEL_CALLS", 1)
    main.save_session("new-v2", pipeline_version="v2", topic="topic")
    def fails(*args):
        assert current_connection().provider == "api"
        with model_request("api"):
            raise RuntimeError("SECRET must not enter sessions")
    graph = SimpleNamespace(invoke=fails)
    settings = ConnectionSettings(provider="api", api_key="SECRET", base_url="https://example.com", model="model")
    first = main.CLIExecution(graph, "new-v2", "v2", settings)
    with pytest.raises(RuntimeError):
        first.invoke({}, {})
    entry = main.load_sessions()["new-v2"]
    assert entry["cli_model_budget"]["model_calls"] == 1
    assert entry["cli_model_budget"]["model_seconds"] >= 0
    assert entry["error"] == "RuntimeError"
    assert "SECRET" not in main.config.SESSIONS_FILE.read_text(encoding="utf-8")
    second = main.CLIExecution(graph, "new-v2", "v2", settings)
    with pytest.raises(BudgetExceeded):
        second.invoke(None, {})
    assert main.load_sessions()["new-v2"]["cli_model_budget"]["model_calls"] == 1
    assert budget_observer.get() is None and current_connection() is None


def test_explicit_extra_budget_preserves_counts_and_other_sessions(monkeypatch):
    monkeypatch.setattr(main.config, "AI_OS_MAX_MODEL_CALLS", 1)
    main.save_session("old-v1", pipeline_version="v1", status="已完成")
    main.save_session("v2", pipeline_version="v2", cli_model_budget={"schema": "cli-model-budget-v1", "model_calls": 1,
        "model_seconds": 12, "additional_model_calls": 0, "additional_seconds": 0})
    def invoke(*args):
        with model_request("api"):
            return {"ok": True}
    runtime = main.CLIExecution(SimpleNamespace(invoke=invoke), "v2", "v2", ConnectionSettings(), extra_calls=2, extra_seconds=30)
    assert runtime.invoke(None, {}) == {"ok": True}
    budget = main.load_sessions()["v2"]["cli_model_budget"]
    assert budget["model_calls"] == 2 and budget["additional_model_calls"] == 2
    assert budget["model_seconds"] >= 12 and budget["additional_seconds"] == 30
    assert "cli_model_budget" not in main.load_sessions()["old-v1"]


def test_v1_runtime_does_not_add_v2_budget():
    main.save_session("old", pipeline_version="v1")
    graph = SimpleNamespace(invoke=lambda *a: {"old": True})
    assert main.CLIExecution(graph, "old", "v1", None).invoke({}, {}) == {"old": True}
    assert "cli_model_budget" not in main.load_sessions()["old"]


def test_corrupt_budget_is_not_reset():
    main.save_session("v2", cli_model_budget={"model_calls": "bad"})
    with pytest.raises(ValueError, match="不能自动重置"):
        main.CLIExecution(None, "v2", "v2", ConnectionSettings())


def test_key_input_refuses_plaintext_fallback(monkeypatch):
    import getpass
    import warnings
    def fallback(*args):
        warnings.warn("echo", getpass.GetPassWarning)
        pytest.fail("不应继续进入明文输入")
    monkeypatch.setattr(getpass, "getpass", fallback)
    from ai_os_connection import ConnectionError
    with pytest.raises(ConnectionError, match="不能遮蔽"):
        main.read_api_key()


@pytest.mark.parametrize("args", [["--sample", "--thread-id", "old"], ["--add-model-calls", "1"],
    ["--thread-id", "old", "--add-model-seconds", "-1"], ["--thread-id", "old", "--add-model-calls", "1001"]])
def test_cli_rejects_invalid_new_options(monkeypatch, args):
    monkeypatch.setattr("sys.argv", ["main.py"] + args)
    with pytest.raises(SystemExit) as exc:
        main.main()
    assert exc.value.code == 2


@pytest.mark.parametrize("args", [["--sample"], ["--ai-os-provider", "codex"], ["--ai-os-key"]])
def test_cli_v1_rejects_v2_options_before_key_input(monkeypatch, args):
    monkeypatch.setattr("sys.argv", ["main.py", "--pipeline", "v1"] + args)
    monkeypatch.setattr(main, "read_api_key", lambda: pytest.fail("v1 不应读取接入密钥"))
    with pytest.raises(SystemExit) as exc:
        main.main()
    assert exc.value.code == 2


def test_cli_missing_checkpoint_does_not_reuse_old_session(monkeypatch):
    main.save_session("old", pipeline_version="v1", topic="original")
    monkeypatch.setattr("sys.argv", ["main.py", "--thread-id", "old"])
    with pytest.raises(SystemExit) as exc:
        main.main()
    assert exc.value.code == 2
    assert main.load_sessions()["old"]["pipeline_version"] == "v1"
    assert "cli_model_budget" not in main.load_sessions()["old"]


def test_cli_mock_sample_to_human_final(monkeypatch, tmp_path):
    import pipeline_v2 as p
    monkeypatch.setattr(p.legacy, "architect", lambda s: {"outline": "outline"})
    monkeypatch.setattr(p.legacy, "researcher", lambda s: {"materials": []})
    monkeypatch.setattr(p.legacy, "writer", lambda s: {"draft": "draft"})
    monkeypatch.setattr(p.legacy, "reviewer", lambda s: {"review_dimensions": {"intent": "pass", "facts": "pass", "reading": "pass"}, "review_verdict": "pass"})
    monkeypatch.setattr(p.legacy, "stylist", lambda s: {"polished": "polished"})
    monkeypatch.setattr(p.legacy, "final_check", lambda s: {"final_check_verdict": "pass", "publication_ready": True, "quality_issues": []})
    def save(state):
        assert state["sample_confirmed"]
        assert state["final_approved_version"] == state["article_version"]
        path = tmp_path / "article.md"
        path.write_text(state["polished"], encoding="utf-8")
        return {"output_path": str(path)}
    monkeypatch.setattr(p.legacy, "save", save)
    answers = iter(["topic", "original input", "END", "确认", "A", "确认"])
    prompts = []
    def user_input(prompt=""):
        prompts.append(prompt)
        return next(answers)
    monkeypatch.setattr("builtins.input", user_input)
    monkeypatch.setattr("sys.argv", ["main.py", "--sample", "--pipeline", "v2", "--ai-os-provider", "api",
        "--ai-os-base-url", "https://example.com/v1", "--ai-os-model", "test", "--ai-os-key"])
    monkeypatch.setattr(main, "read_api_key", lambda: "SECRET")
    main.main()
    sessions = main.load_sessions()
    assert len(sessions) == 1
    entry = next(iter(sessions.values()))
    assert entry["status"] == "已完成"
    assert sum("摘要无误" in prompt for prompt in prompts) == 1
    assert sum("保存本地成稿" in prompt for prompt in prompts) == 1
    assert "SECRET" not in main.config.SESSIONS_FILE.read_text(encoding="utf-8")
    assert "SECRET" not in (tmp_path / "pipeline_v2.json").read_text(encoding="utf-8")
    assert entry["cli_model_budget"]["model_calls"] == 0
    assert (tmp_path / "article.md").read_text(encoding="utf-8") == "polished"
