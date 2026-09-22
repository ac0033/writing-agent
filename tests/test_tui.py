"""TUI操作真实任务管理器，模拟模型但不自动越过人工确认。"""
import asyncio
import time

import pytest


def wait_idle(controller):
    for _ in range(300):
        status = controller.status()
        if status.get("status") not in {"pending", "running"}:
            return status
        time.sleep(0.01)
    raise AssertionError("任务未停在预期边界")


def test_controller_user_confirmation_and_stale_view(tmp_path, monkeypatch):
    import config
    from service.tui_controller import TuiController
    monkeypatch.setattr(config, "TOPIC_DIR", tmp_path / "topics")
    controller = TuiController(tmp_path / "tui")
    controller.start("测试主题", "这是一份模拟测试观点，并非真实验收。")
    status = wait_idle(controller)
    assert status["interrupt"]["kind"] == "summary"
    assert not any(x in status["progress"] for x in ("writer", "architect"))
    with pytest.raises(ValueError, match="先查看"):
        controller.approve()
    controller.show_interrupt(status["interrupt"])
    old = dict(controller.displayed)
    controller.send("请面向初学者解释")
    revised = wait_idle(controller)
    assert revised["interrupt"]["kind"] == "summary"
    assert revised["summary_version"] > old["summary_version"]
    controller.show_interrupt(old)
    with pytest.raises(ValueError, match="版本已经变化"):
        controller.approve()
    assert controller.status()["status"] == "awaiting_human"


def test_task_worker_inherits_context_but_does_not_persist_secrets(tmp_path, monkeypatch):
    from contextvars import ContextVar
    from service.writing_server import TaskManager
    marker = ContextVar("test_connection_secret", default="")
    observed = []
    manager = TaskManager(tmp_path / "tasks.json", on_saved=None)
    manager.tasks["t"] = {"task_id": "t", "status": "pending"}
    token = marker.set("secret-only-in-memory")
    try:
        manager._spawn("t", lambda *args: observed.append(marker.get()))
    finally:
        marker.reset(token)
    manager._threads["t"].join(2)
    assert observed == ["secret-only-in-memory"]
    assert "secret-only-in-memory" not in (tmp_path / "tasks.json").read_text()


def test_terminal_ui_shows_confirmation_and_masks_key(tmp_path):
    from tui import WritingApp
    from textual.widgets import Input, Button

    async def scenario():
        app = WritingApp(tmp_path)
        async with app.run_test(size=(110, 38)) as pilot:
            assert app.query_one("#approve", Button).disabled
            await pilot.click("#connection")
            await pilot.pause()
            assert app.screen.query_one("#api-key", Input).password
            app.screen.query_one("#api-key", Input).value = "test-secret"
            await pilot.click("#cancel-connection")
            await pilot.pause()
            assert app.settings.api_key == ""
    asyncio.run(scenario())


def test_role_override_only_touches_process_environment():
    from tui import apply_role_overrides
    env = {}
    apply_role_overrides(["reviewer=claude:claude-fable-5-1", "researcher=claude"], env)
    assert env == {"WRITING_REVIEWER_PROVIDER": "claude", "WRITING_REVIEWER_MODEL": "claude-fable-5-1",
                   "WRITING_RESEARCHER_PROVIDER": "claude", "WRITING_RESEARCHER_MODEL": ""}
    for bad in ("editor=claude", "reviewer=openai", "reviewer", "=claude"):
        with pytest.raises(SystemExit):
            apply_role_overrides([bad], {})


def test_final_confirmation_view_strips_internal_comments_but_binds_raw_payload(tmp_path):
    from tui import WritingApp
    from textual.widgets import Button, RichLog
    from service.tui_controller import TuiController
    controller = TuiController(tmp_path)
    payload = {"kind": "final", "pipeline_version": "v2", "requires_human": True, "summary_version": 2,
               "article_version": 8, "polished": "<!-- 润色说明：内部记录 -->\n\n# 标题\n\n正文第一段。"}
    controller.manager.tasks["a"] = {"task_id": "a", "thread_id": "a", "pipeline_version": "v2",
                                     "status": "awaiting_human", "topic": "t", "interrupt": payload}
    controller.select("a")
    async def scenario():
        app = WritingApp(tmp_path, controller=controller)
        async with app.run_test(size=(110, 38)) as pilot:
            app.refresh_status()
            await pilot.pause()
            text = "\n".join(str(line.text) for line in app.query_one("#chat", RichLog).lines)
            assert "润色说明" not in text and "正文第一段" in text and "第 8 版" in text
            assert str(app.query_one("#approve", Button).label) == "确认署名并保存本地"
            assert controller.displayed == payload
    asyncio.run(scenario())


def test_role_screen_applies_task_override_and_routes_are_shown(tmp_path, monkeypatch):
    """页面改分工 → 任务登记簿；实际接入记录（routes）出现在对话区与状态栏。"""
    from tui import WritingApp, RoleScreen
    from textual.widgets import Button, Input, RichLog, Select, Static
    from service.tui_controller import TuiController
    controller = TuiController(tmp_path)
    controller.manager.tasks["a"] = {"task_id": "a", "thread_id": "a", "pipeline_version": "v2", "status": "running", "topic": "t"}
    controller.select("a")
    async def scenario():
        app = WritingApp(tmp_path, controller=controller)
        async with app.run_test(size=(130, 50)) as pilot:
            await pilot.click("#roles")
            await pilot.pause()
            assert isinstance(app.screen, RoleScreen)
            app.screen.query_one("#role-provider-reviewer", Select).value = "claude"
            app.screen.query_one("#role-model-reviewer", Input).value = "claude-fable-5-1"
            await pilot.click("#apply-roles")
            for _ in range(100):
                await pilot.pause(0.02)
                if controller.manager.tasks["a"].get("role_settings") and not app._action_pending:
                    break
            task = controller.manager.tasks["a"]
            assert task["role_settings"] == {"reviewer": {"provider": "claude", "model": "claude-fable-5-1"}}
            assert task["status"] == "running"  # 运行中允许设置，不打断任务
            import re
            def chat_text():
                # RichLog 按宽度折行，比较前去掉所有空白
                return re.sub(r"\s+", "", "".join(str(l.text) for l in app.query_one("#chat", RichLog).lines))
            text = chat_text()
            assert "reviewer=ClaudeCode/claude-fable-5-1" in text and "下一节点边界" in text
            # 模拟 runner 记入的实际接入，页面应同步显示并更新状态栏
            task["routes"] = [{"role": "reviewer", "provider": "claude", "model": "claude-fable-5-1", "reason": "Codex 最低窗口剩余 2%；回退第 1 顺位", "at": "x"}]
            app.refresh_status()
            await pilot.pause()
            text = chat_text()
            assert "reviewer已接入ClaudeCode/claude-fable-5-1" in text
            assert "reviewer 已接入 Claude Code / claude-fable-5-1" in str(app.query_one("#status", Static).renderable)
            app.refresh_status()
            await pilot.pause()
            assert text.count("reviewer已接入") == chat_text().count("reviewer已接入")
    asyncio.run(scenario())
