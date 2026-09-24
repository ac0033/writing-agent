"""追加预算只来自明确表单确认，不启动模型，并绑定打开表单时的任务。"""
import asyncio
import json

import pytest


def controller_with_tasks(tmp_path, status="failed"):
    from service.tui_controller import TuiController
    controller = TuiController(tmp_path)
    for identity in ("a", "b"):
        controller.manager.tasks[identity] = {"task_id": identity, "thread_id": identity,
            "pipeline_version": "v2", "status": status, "topic": "测试任务" + identity,
            "model_calls": 7, "model_seconds": 12.5}
    controller.select("a")
    return controller


@pytest.mark.parametrize("status", ["failed", "interrupted", "awaiting_human"])
def test_budget_persists_explicit_grant_without_resuming(tmp_path, monkeypatch, status):
    controller = controller_with_tasks(tmp_path, status)
    monkeypatch.setattr(controller.manager, "resume", lambda *a: pytest.fail("追加预算不能默认续跑"))
    snapshot = controller.budget_snapshot()
    assert snapshot["model_calls"] == 7 and snapshot["model_seconds"] == 12.5
    result = controller.execute("extend_budget", (3, 90), snapshot["task_id"], None)
    task = controller.manager.tasks["a"]
    assert task["additional_model_calls"] == 3 and task["additional_seconds"] == 90
    assert task["status"] == status and task["model_calls"] == 7
    assert task["budget_events"][-1]["source"] == "user"
    assert not result["resumed"]
    persisted = json.loads((tmp_path / "tasks.json").read_text(encoding="utf-8"))
    assert persisted["a"]["budget_events"][-1]["calls"] == 3
    assert "budget_events" not in controller.manager.tasks["b"]


@pytest.mark.parametrize("status", ["running", "pending", "completed"])
def test_budget_unavailable_while_running_or_completed(tmp_path, status):
    controller = controller_with_tasks(tmp_path, status)
    with pytest.raises(ValueError, match="失败、暂停或等待人工"):
        controller.budget_snapshot()
    with pytest.raises(ValueError):
        controller.execute("extend_budget", (1, 0), "a", None)
    assert "budget_events" not in controller.manager.tasks["a"]


def test_form_target_and_latest_state_are_rechecked(tmp_path):
    controller = controller_with_tasks(tmp_path)
    snapshot = controller.budget_snapshot()
    controller.select("b")
    with pytest.raises(ValueError, match="任务已经切换"):
        controller.execute("extend_budget", (1, 0), snapshot["task_id"], None)
    controller.select("a")
    controller.manager.tasks["a"]["status"] = "running"
    with pytest.raises(ValueError):
        controller.execute("extend_budget", (1, 0), snapshot["task_id"], None)
    assert all("budget_events" not in task for task in controller.manager.tasks.values())


@pytest.mark.parametrize("calls,seconds", [(0, 0), (-1, 0), (121, 0), (0, 86401), (True, 1), ("3", 1)])
def test_invalid_budget_never_persists(tmp_path, calls, seconds):
    controller = controller_with_tasks(tmp_path)
    with pytest.raises(ValueError):
        controller.execute("extend_budget", (calls, seconds), "a", None)
    assert "budget_events" not in controller.manager.tasks["a"]


def test_terminal_budget_form_requires_values_and_does_not_resume(tmp_path, monkeypatch):
    from tui import WritingApp, BudgetScreen
    from textual.widgets import Button, Input
    controller = controller_with_tasks(tmp_path)
    monkeypatch.setattr(controller.manager, "resume", lambda *a: pytest.fail("不能自动续跑"))
    async def scenario():
        app = WritingApp(tmp_path, controller=controller)
        async with app.run_test(size=(120, 42)) as pilot:
            app.refresh_status()
            assert not app.query_one("#budget", Button).disabled
            await pilot.click("#budget")
            await pilot.pause()
            assert isinstance(app.screen, BudgetScreen)
            assert app.screen.snapshot["task_id"] == "a"
            assert app.screen.query_one("#budget-calls", Input).value == ""
            confirm = app.screen.query_one("#apply-budget", Button)
            assert confirm.disabled
            initial_region = confirm.region
            await pilot.click("#apply-budget")
            await pilot.pause()
            assert isinstance(app.screen, BudgetScreen)
            assert "budget_events" not in controller.manager.tasks["a"]
            assert not confirm.has_class("-active")
            app.screen.query_one("#budget-calls", Input).value = "2"
            app.screen.query_one("#budget-seconds", Input).value = "60"
            await pilot.pause()
            assert not confirm.disabled
            assert confirm.region == initial_region
            await pilot.click("#apply-budget")
            for _ in range(100):
                await pilot.pause(0.02)
                if controller.manager.tasks["a"].get("budget_events") and not app._action_pending:
                    break
            task = controller.manager.tasks["a"]
            assert task["status"] == "failed"
            assert "additional_model_calls" in task, (type(app.screen).__name__, app._action_pending,
                app.chat_text())
            assert task["additional_model_calls"] == 2 and task["additional_seconds"] == 60
            assert len(task["budget_events"]) == 1
    asyncio.run(scenario())


def test_terminal_cancel_or_switch_does_not_grant_to_wrong_task(tmp_path):
    from tui import WritingApp, BudgetScreen
    from textual.widgets import Input
    controller = controller_with_tasks(tmp_path)
    async def scenario():
        app = WritingApp(tmp_path, controller=controller)
        errors = []
        original_failed = app.action_failed
        def failed(message):
            errors.append(message)
            original_failed(message)
        app.action_failed = failed
        async with app.run_test(size=(120, 42)) as pilot:
            app.refresh_status()
            await pilot.click("#budget")
            await pilot.pause()
            app.screen.query_one("#budget-calls", Input).value = "4"
            await pilot.click("#cancel-budget")
            await pilot.pause()
            assert "budget_events" not in controller.manager.tasks["a"]
            await pilot.click("#budget")
            await pilot.pause()
            assert isinstance(app.screen, BudgetScreen)
            controller.select("b")
            app.screen.query_one("#budget-calls", Input).value = "4"
            await pilot.click("#apply-budget")
            for _ in range(100):
                await pilot.pause(0.02)
                if errors and not app._action_pending:
                    break
            assert any("任务已经切换" in message for message in errors)
            assert all("budget_events" not in task for task in controller.manager.tasks.values())
    asyncio.run(scenario())


def paused_controller(tmp_path, kind="decision", pause_reason="总调度预算已用完"):
    controller = controller_with_tasks(tmp_path, "awaiting_human")
    controller.manager.tasks["a"]["interrupt"] = {"kind": kind, "requires_human": True, "pause_reason": pause_reason,
                                                  "ai_os_steps": 36, "question": pause_reason or "作者需要决定引用方式"}
    return controller


def test_paused_ai_os_accepts_step_and_revision_grants_and_resumes(tmp_path, monkeypatch):
    """图内的调度步数/修订次数只能交回暂停点；交回即从暂停点继续，且不写成任务级请求预算。"""
    from service.writing_server import TaskManager
    controller = paused_controller(tmp_path)
    resumed = []
    monkeypatch.setattr(controller.manager, "resume", lambda task_id, decision: resumed.append((task_id, decision)) or "ok")
    snapshot = controller.budget_snapshot()
    assert snapshot["paused"] and snapshot["ai_os_steps"] == 36 and "调度预算" in snapshot["pause_reason"]
    result = controller.execute("extend_budget", (0, 0, 6, 1), "a", None)
    assert result["resumed"] and result["added_steps"] == 6 and result["added_revisions"] == 1
    assert resumed == [("a", {"feedback": "", "additional_steps": 6, "additional_revisions": 1})]
    # 服务层校验与图节点都接受这个 decision：feedback 可为空，预算增量为整数。
    TaskManager._validate_decision(controller.manager.tasks["a"], resumed[0][1])
    task = controller.manager.tasks["a"]
    assert task["budget_events"][-1]["steps"] == 6 and task["budget_events"][-1]["revisions"] == 1
    assert "additional_model_calls" not in task and task["model_calls"] == 7
    assert controller.displayed is None


def test_paused_form_can_combine_request_budget_with_steps(tmp_path, monkeypatch):
    controller = paused_controller(tmp_path)
    monkeypatch.setattr(controller.manager, "resume", lambda *a: "ok")
    result = controller.execute("extend_budget", (2, 0, 3, 0), "a", None)
    task = controller.manager.tasks["a"]
    assert task["additional_model_calls"] == 2 and result["resumed"]
    assert [e.get("calls", e.get("steps")) for e in task["budget_events"]] == [2, 3]


@pytest.mark.parametrize("status,interrupt", [
    ("failed", None),
    ("interrupted", None),
    ("awaiting_human", {"kind": "final", "polished": "稿", "summary_version": 1, "article_version": 1}),
    ("awaiting_human", {"kind": "decision", "question": "作者需要决定引用方式", "pause_reason": ""}),
])
def test_steps_rejected_unless_ai_os_is_paused_on_budget(tmp_path, monkeypatch, status, interrupt):
    controller = controller_with_tasks(tmp_path, status)
    controller.manager.tasks["a"]["interrupt"] = interrupt
    monkeypatch.setattr(controller.manager, "resume", lambda *a: pytest.fail("未暂停不能借追加预算续跑"))
    assert not controller.budget_snapshot()["paused"]
    with pytest.raises(ValueError, match="暂停"):
        controller.execute("extend_budget", (0, 0, 3, 0), "a", None)
    with pytest.raises(ValueError, match="暂停"):
        controller.execute("extend_budget", (1, 0, 0, 2), "a", None)
    assert "budget_events" not in controller.manager.tasks["a"]


@pytest.mark.parametrize("steps,revisions", [(101, 0), (0, 11), (-1, 0), (True, 0), ("3", 0)])
def test_invalid_step_or_revision_grants_never_persist(tmp_path, monkeypatch, steps, revisions):
    controller = paused_controller(tmp_path)
    monkeypatch.setattr(controller.manager, "resume", lambda *a: pytest.fail("非法数值不能续跑"))
    with pytest.raises(ValueError):
        controller.execute("extend_budget", (0, 0, steps, revisions), "a", None)
    assert "budget_events" not in controller.manager.tasks["a"]


def test_terminal_paused_form_offers_step_fields_and_resumes_from_pause(tmp_path, monkeypatch):
    from tui import WritingApp, BudgetScreen
    from textual.widgets import Button, Input
    controller = paused_controller(tmp_path)
    resumed = []
    monkeypatch.setattr(controller.manager, "resume", lambda task_id, decision: resumed.append(decision) or "ok")
    async def scenario():
        app = WritingApp(tmp_path, controller=controller)
        async with app.run_test(size=(120, 50)) as pilot:
            app.refresh_status()
            assert not app.query_one("#budget", Button).disabled
            await pilot.click("#budget")
            await pilot.pause()
            assert isinstance(app.screen, BudgetScreen)
            assert "续跑" in str(app.screen.query_one("#apply-budget", Button).label)
            app.screen.query_one("#budget-steps", Input).value = "6"
            await pilot.pause()
            confirm = app.screen.query_one("#apply-budget", Button)
            assert not confirm.disabled
            await pilot.click("#apply-budget")
            for _ in range(100):
                await pilot.pause(0.02)
                if resumed and not app._action_pending:
                    break
            assert resumed == [{"feedback": "", "additional_steps": 6, "additional_revisions": 0}]
            assert controller.manager.tasks["a"]["budget_events"][-1]["steps"] == 6
    asyncio.run(scenario())


def test_unpaused_form_has_no_step_fields(tmp_path):
    from tui import WritingApp, BudgetScreen
    from textual.css.query import NoMatches
    from textual.widgets import Input
    controller = controller_with_tasks(tmp_path)
    async def scenario():
        app = WritingApp(tmp_path, controller=controller)
        async with app.run_test(size=(120, 42)) as pilot:
            app.refresh_status()
            await pilot.click("#budget")
            await pilot.pause()
            assert isinstance(app.screen, BudgetScreen)
            with pytest.raises(NoMatches):
                app.screen.query_one("#budget-steps", Input)
            assert "不续跑" in str(app.screen.query_one("#apply-budget").label)
    asyncio.run(scenario())
