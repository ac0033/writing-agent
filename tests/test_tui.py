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


def no_cli():
    """测试不去探测本机 CLI：模拟一台没装 Codex / Claude Code 的机器。"""
    from ai_os_setup import CliInfo
    return {"codex": CliInfo("codex", False, reason="未检测到"), "claude": CliInfo("claude", False, reason="未检测到")}


def both_cli():
    from ai_os_setup import CliInfo
    return {"codex": CliInfo("codex", True, "codex-cli 1.0", "使用本机登录，可自动接入",
                             (("gpt-6-astra", "GPT-6-Astra", True), ("gpt-6-sol", "GPT-6-Sol", False))),
            "claude": CliInfo("claude", True, "2.1 (Claude Code)", "使用本机登录，可自动接入")}


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
        app = WritingApp(tmp_path, detector=no_cli)
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
    from textual.widgets import Button
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
            text = app.chat_text()
            assert "润色说明" not in text and "正文第一段" in text and "第 8 版" in text
            assert str(app.query_one("#approve", Button).label) == "确认署名并保存本地"
            assert controller.displayed == payload
    asyncio.run(scenario())


def test_role_screen_applies_task_override_and_routes_are_shown(tmp_path, monkeypatch):
    """页面改分工 → 任务登记簿；实际接入记录（routes）出现在对话区与状态栏。"""
    from tui import WritingApp, RoleScreen
    from textual.widgets import Button, Input, Select, Static
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
                return re.sub(r"\s+", "", app.chat_text())
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


def test_stage_shows_only_relevant_actions_and_render_switch(tmp_path):
    """终审阶段只显示“退回修改”和“确认署名并保存本地”；点击正文切原文，渲染关闭后停留原文。"""
    from tui import WritingApp, ACTION_IDS
    from service.tui_controller import TuiController
    controller = TuiController(tmp_path)
    payload = {"kind": "final", "pipeline_version": "v2", "requires_human": True, "summary_version": 1,
               "article_version": 3, "polished": "# 标题\n\n**正文**第一段。"}
    controller.manager.tasks["a"] = {"task_id": "a", "thread_id": "a", "pipeline_version": "v2",
                                     "status": "awaiting_human", "topic": "t", "interrupt": payload}
    async def scenario():
        app = WritingApp(tmp_path, controller=controller)
        async with app.run_test(size=(120, 40)) as pilot:
            assert app.query_one("#topic").display and not app.query_one("#approve").display
            controller.select("a")
            app.refresh_status()
            await pilot.pause()
            shown = {key for key in ACTION_IDS if app.query_one("#" + key).display}
            assert shown == {"send", "approve"}
            assert str(app.query_one("#send").label) == "退回修改"
            assert not app.query_one("#topic").display
            view, raw = app.query_one("#chat-view"), app.query_one("#chat-raw")
            await pilot.click("#chat-view")
            await pilot.pause()
            assert raw.display and not view.display and "**正文**" in raw.text
            await pilot.click("#render-toggle")
            await pilot.pause()
            assert not app.auto_render and raw.display
            await pilot.pause(0.3)  # 按钮按下后约 0.2 秒内的再次点击会被框架忽略
            await pilot.click("#render-toggle")
            await pilot.pause()
            assert view.display and not raw.display
    asyncio.run(scenario())


async def _detected(app, pilot):
    for _ in range(100):
        await pilot.pause(0.02)
        if app.clis is not None:
            return app.screen
    raise AssertionError("CLI 检测未完成")


def test_start_page_without_clis_offers_only_apis(tmp_path):
    """没装 Codex / Claude Code 的机器：CLI 选项不可选、没有“自动”，默认落在 DeepSeek，第二步要密钥。"""
    from tui import WritingApp, ConnectionScreen
    from textual.widgets import RadioButton
    async def scenario():
        app = WritingApp(tmp_path, choose_connection=True, detector=no_cli)
        async with app.run_test(size=(130, 50)) as pilot:
            screen = await _detected(app, pilot)
            assert isinstance(screen, ConnectionScreen) and not screen.query("#cancel-connection")
            assert not screen.query("#opt-auto")
            assert screen.query_one("#opt-codex", RadioButton).disabled and screen.query_one("#opt-claude", RadioButton).disabled
            assert screen.query_one("#opt-deepseek", RadioButton).value and screen.query("#opt-anthropic")
            assert not screen.query_one("#step-model").display
            await pilot.click("#next-step")
            await pilot.pause()
            assert screen.query_one("#step-model").display and screen.query_one("#api-key").display
    asyncio.run(scenario())


def test_start_page_two_steps_lists_models_of_chosen_provider(tmp_path):
    """先选接入方式，第二步只列该方式的模型；手动输入的名字规范后接入，选择记盘但不含密钥。"""
    import json
    from tui import WritingApp, ConnectionScreen
    async def scenario():
        app = WritingApp(tmp_path, choose_connection=True, detector=both_cli)
        async with app.run_test(size=(130, 50)) as pilot:
            screen = await _detected(app, pilot)
            assert screen.query_one("#opt-codex").value  # 自动未启用时默认第一个可用 CLI
            await pilot.click("#next-step")
            await pilot.pause()
            codex_ids = [screen.query_one("#models").get_option_at_index(i).id
                         for i in range(screen.query_one("#models").option_count)]
            assert codex_ids == [screen.DEFAULT, "gpt-6-astra", "gpt-6-sol", screen.MANUAL]
            await pilot.click("#prev-step")
            await pilot.pause()
            await pilot.click("#opt-claude")
            await pilot.pause()
            await pilot.click("#next-step")
            await pilot.pause()
            listing = screen.query_one("#models")
            ids = [listing.get_option_at_index(i).id for i in range(listing.option_count)]
            assert "gpt-6-astra" not in ids and "claude-opus-5-5" in ids
            listing.highlighted = ids.index(screen.MANUAL)
            await pilot.pause()
            assert screen.query_one("#model").display
            screen.query_one("#model").value = "Opus 5.5"
            await pilot.click("#apply-connection")
            for _ in range(100):
                await pilot.pause(0.02)
                if not isinstance(app.screen, ConnectionScreen):
                    break
            assert app.settings.provider == "claude" and app.settings.model == "claude-opus-5-5"
            assert "已规范为 claude-opus-5-5" in app.chat_text()
            saved = json.loads((tmp_path / "ai_os_choice.json").read_text(encoding="utf-8"))
            assert saved["model"] == "claude-opus-5-5" and "api_key" not in saved
    asyncio.run(scenario())


def test_auto_option_only_when_enabled_locally(tmp_path, monkeypatch):
    import config
    from tui import WritingApp
    monkeypatch.setattr(config, "AI_OS_AUTO_ENABLED", True)
    async def scenario():
        app = WritingApp(tmp_path, choose_connection=True, detector=both_cli)
        # 小终端：自动模式的两张模型表加起来比屏幕高，按钮必须仍在屏内、能点进主页面。
        async with app.run_test(size=(120, 24)) as pilot:
            screen = await _detected(app, pilot)
            assert screen.query_one("#opt-auto").value
            await pilot.click("#next-step")
            await pilot.pause()
            assert screen.query_one("#models").display and screen.query_one("#models-claude").display
            assert screen.query_one("#apply-connection").region.bottom <= app.screen.size.height
            await pilot.click("#apply-connection")
            for _ in range(100):
                await pilot.pause(0.02)
                if app.screen is not screen:
                    break
            assert app.screen is not screen and app.settings.provider == "auto"
    asyncio.run(scenario())


def test_anthropic_compatible_connection_prepares_and_invokes(monkeypatch):
    """Anthropic 兼容 API：去掉多填的 /v1，按已读到的模型列表规范名字；调用走官方 SDK 并拒收截断输出。"""
    import anthropic
    import ai_os_connection as c
    from ai_os_setup import prepare_connection
    settings, _ = prepare_connection({"provider": "anthropic", "base_url": "https://x.example/anthropic/v1",
                                      "api_key": "k", "model": "Opus 5.5", "models": ["claude-opus-5-5"]}, {})
    assert (settings.provider, settings.model) == ("anthropic", "claude-opus-5-5")
    assert c.anthropic_base_url(settings.base_url) == "https://x.example/anthropic"
    calls = []

    class Block:
        type, text = "text", "调度结果"

    class Reply:
        def __init__(self, stop):
            self.stop_reason, self.content, self.model = stop, [Block()], "claude-opus-5-5"

    class Fake:
        stop = "end_turn"

        def __init__(self, **kwargs):
            calls.append(kwargs)
            self.messages = self

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def create(self, **kwargs):
            calls.append(kwargs)
            return Reply(Fake.stop)

    monkeypatch.setattr(anthropic, "Anthropic", Fake)
    reply = c.invoke_connection("系统", "用户", settings=settings)
    assert reply.text == "调度结果" and calls[0]["base_url"] == "https://x.example/anthropic"
    assert calls[1]["system"] == "系统" and calls[1]["max_tokens"] > 0
    Fake.stop = "max_tokens"
    with pytest.raises(c.ConnectionError, match="截断"):
        c.invoke_connection("系统", "用户", settings=settings)


def test_model_normalization_rules():
    from ai_os_setup import normalize_model, claude_catalog
    from ai_os_connection import ConnectionError
    codex = ["gpt-6-astra", "gpt-6-sol"]
    assert normalize_model("claude", "Opus 5.5", claude_catalog())[0] == "claude-opus-5-5"
    assert normalize_model("claude", "ＳＯＮＮＥＴ５", claude_catalog())[0] == "claude-sonnet-5"
    assert normalize_model("claude", "opus", claude_catalog()) == ("opus", "")
    assert normalize_model("codex", "GPT 6 Astra", codex, True)[0] == "gpt-6-astra"
    assert normalize_model("codex", "", codex, True) == ("", "")
    with pytest.raises(ConnectionError, match="gpt-6-sol"):
        normalize_model("codex", "gpt-5-sol", codex, True)
    with pytest.raises(ConnectionError, match="多个模型"):
        normalize_model("codex", "gpt 6", codex, True)
    with pytest.raises(ConnectionError, match="不像 Claude"):
        normalize_model("claude", "你好", claude_catalog())


def test_sample_toggle_shows_empty_box_until_checked(tmp_path):
    from tui import WritingApp
    async def scenario():
        app = WritingApp(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            toggle = app.query_one("#sample")
            assert str(toggle._button) == "☐"
            await pilot.click("#sample")
            await pilot.pause()
            assert toggle.value and str(toggle._button) == "☑"
            await pilot.press("f1")
            await pilot.pause()
            assert type(app.screen).__name__ == "KeysScreen"
            await pilot.press("escape")
            await pilot.pause()
            assert type(app.screen).__name__ != "KeysScreen"
    asyncio.run(scenario())


@pytest.mark.parametrize("step,key", [(1, "ctrl+q"), (2, "ctrl+q"), (1, "ctrl+c"), (2, "ctrl+c"), (1, "button")])
def test_start_page_can_always_quit(tmp_path, step, key):
    """开始页是弹窗：应用级普通快捷键被挡住，Ctrl+Q 必须是优先绑定；接入页的 Ctrl+C 和“退出”按钮也能退出。"""
    from tui import WritingApp
    async def scenario():
        app = WritingApp(tmp_path, choose_connection=True, detector=both_cli)
        async with app.run_test(size=(120, 40)) as pilot:
            await _detected(app, pilot)
            if step == 2:
                await pilot.click("#next-step")
                await pilot.pause()
            if key == "button":
                await pilot.click("#quit-app")
            else:
                await pilot.press(key)
            for _ in range(60):
                await pilot.pause(0.05)
                if not app.is_running:
                    break
            assert not app.is_running
    asyncio.run(scenario())


def _task_dir(path, identity, task):
    import json
    path.mkdir(parents=True, exist_ok=True)
    (path / "tasks.json").write_text(json.dumps({identity: {"task_id": identity, "thread_id": identity, **task}},
                                                ensure_ascii=False), encoding="utf-8")
    return path


def test_history_lists_linked_dirs_and_readonly_articles(tmp_path):
    """历史按来源分组：本机任务、登记的任务目录（选中即切换目录）、已发布文章（只读，无确认/保存按钮）。"""
    import json
    from tui import WritingApp, HistoryScreen, ACTION_IDS
    home = tmp_path / "home"
    other = _task_dir(tmp_path / "blog2" / "run", "b2", {"status": "completed", "topic": "旧文章二",
                                                          "output_path": str(tmp_path / "a.md")})
    article = tmp_path / "blog1" / "article.md"
    article.parent.mkdir(parents=True)
    article.write_text("# 已发布标题\n\n正文第一段。", encoding="utf-8")
    home.mkdir()
    (home / "history_sources.json").write_text(json.dumps({"task_dirs": [str(other)],
        "articles": [{"path": str(article)}]}, ensure_ascii=False), encoding="utf-8")
    async def scenario():
        app = WritingApp(home)
        async with app.run_test(size=(130, 40)) as pilot:
            await pilot.click("#history")
            await pilot.pause()
            assert isinstance(app.screen, HistoryScreen)
            ids = [o.id for o in app.screen.query_one("#history-list")._options if o.id]
            assert ids == ["task|1|b2", "article|0"]
            app.screen.dismiss("article|0")
            await pilot.pause()
            assert "已发布标题" in app.chat_text() and not app.controller.task_id
            assert not any(app.query_one("#" + key).display for key in ACTION_IDS)
            assert not app.query_one("#composer").display
            app.history_chosen("task|1|b2")
            await pilot.pause()
            assert app.directory.resolve() == other.resolve() and app.controller.task_id == "b2"
            assert (other / ".ui.lock").exists() and app._viewing_article is None
            assert app.query_one("#publish-preview").display
    asyncio.run(scenario())


def test_v1_final_feedback_maps_to_content_route_and_forced_pass_is_disclosed(tmp_path):
    """旧流程终审只认 approve/content/style：退回修改发 content；强制放行的稿不能写成“已通过”。"""
    from tui import WritingApp
    from service.tui_controller import TuiController
    controller = TuiController(tmp_path)
    payload = {"kind": "final", "requires_human": True, "polished": "# 旧稿\n\n正文。", "forced_pass": True,
               "quality_issues": ["引文未核实"]}
    controller.manager.tasks["v1"] = {"task_id": "v1", "thread_id": "v1", "status": "awaiting_human",
                                      "topic": "旧流程", "interrupt": payload}
    controller.select("v1")
    resumed = []
    controller.manager.resume = lambda task_id, decision: resumed.append(decision) or "ok"
    async def scenario():
        app = WritingApp(tmp_path, controller=controller)
        async with app.run_test(size=(120, 40)) as pilot:
            app.refresh_status()
            await pilot.pause()
            text = app.chat_text()
            assert "审核未通过" in text and "引文未核实" in text and "已通过原意" not in text
    asyncio.run(scenario())
    controller.send("请重写第二节")
    assert resumed == [{"route": "content", "feedback": "请重写第二节"}]
