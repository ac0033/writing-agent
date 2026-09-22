"""终端真实交互闭环与并发、确认、隐私边界。所有LLM均mock，发布用替身。"""
import asyncio
import json
import threading

import pytest


@pytest.mark.parametrize("decision", [
    {"approved": False, "route": "approve"},
    {"approved": True, "expected_summary_version": 1, "feedback": "请改重点"},
])
def test_summary_cannot_bypass_displayed_approval_or_swallow_feedback(decision):
    from service.writing_server import TaskManager
    task = {"pipeline_version": "v2", "interrupt": {"kind": "summary", "summary_version": 1}}
    with pytest.raises(ValueError):
        TaskManager._validate_decision(task, decision)


def test_sample_choice_requires_explicit_displayed_version():
    from service.writing_server import TaskManager
    task = {"pipeline_version": "v2", "interrupt": {"kind": "sample", "summary_version": 2, "options": {"A": "a", "B": "b"}}}
    with pytest.raises(ValueError, match="所审阅版本"):
        TaskManager._validate_decision(task, {"choice": "A"})
    with pytest.raises(ValueError, match="版本已经变化"):
        TaskManager._validate_decision(task, {"choice": "A", "expected_summary_version": 1})
    TaskManager._validate_decision(task, {"choice": "A", "expected_summary_version": 2})


def test_controller_action_cannot_switch_target_after_click(tmp_path):
    from service.tui_controller import TuiController
    controller = TuiController(tmp_path)
    controller.manager.tasks.update({"a": {}, "b": {}})
    controller.select("a")
    snapshot = controller.action_snapshot()
    controller.select("b")
    with pytest.raises(ValueError, match="任务已经切换"):
        controller.execute("approve", (), *snapshot)


def test_runner_and_task_error_redact_session_key(tmp_path):
    from ai_os_connection import ConnectionSettings, use_connection
    from service.writing_server import TaskManager
    manager = TaskManager(tmp_path / "tasks.json", on_saved=None)
    manager.tasks["t"] = {"task_id": "t", "status": "pending"}
    secret = "session-only-private-test-value"
    def fail(*args):
        raise RuntimeError("transport response accidentally included " + secret)
    with use_connection(ConnectionSettings(provider="api", api_key=secret)):
        manager._spawn("t", fail)
    manager._threads["t"].join(5)
    persisted = (tmp_path / "tasks.json").read_text(encoding="utf-8")
    assert secret not in persisted
    assert "[REDACTED]" in persisted


def test_save_gate_does_not_hold_lock_while_merging_feedback(monkeypatch):
    import pipeline_v2
    from service import runner
    entered, release = threading.Event(), threading.Event()
    lock = threading.RLock()
    def merge(state):
        entered.set()
        assert release.wait(5)
        return {"shared_summary": "changed"}
    monkeypatch.setattr(pipeline_v2, "_drain_feedback", merge)
    task = {"pending_feedback": [{"id": "one"}]}
    results = []
    def work():
        token = runner.task_state_lock.set(lock)
        try:
            results.append(runner._commit_save_gate(task, {}, lambda t: None))
        finally:
            runner.task_state_lock.reset(token)
    worker = threading.Thread(target=work)
    worker.start()
    try:
        assert entered.wait(5)
        assert lock.acquire(timeout=0.5), "摘要模型执行期间不应阻塞更新队列"
        task["pending_feedback"].append({"id": "two"})
        lock.release()
    finally:
        release.set()
        worker.join(5)
    assert results == [{"shared_summary": "changed"}]
    assert not task.get("save_started")


def test_save_gate_checks_arrival_after_empty_snapshot(monkeypatch):
    import pipeline_v2
    from service import runner
    task = {"pending_feedback": []}
    calls = []
    def merge(state):
        calls.append(1)
        if len(calls) == 1:
            task["pending_feedback"].append({"id": "raced"})
            return {}
        return {"shared_summary": "late feedback"}
    monkeypatch.setattr(pipeline_v2, "_drain_feedback", merge)
    assert runner._commit_save_gate(task, {}, lambda t: None)["shared_summary"] == "late feedback"
    assert len(calls) == 2 and not task.get("save_started")


def test_terminal_full_flow_summary_revision_sample_final_and_save(tmp_path, monkeypatch):
    import config
    import graph
    from tui import WritingApp
    from textual.widgets import RichLog, Button, Checkbox, Input, TextArea
    from tools import publishing
    import hashlib
    from pathlib import Path
    publications = []
    def preview(path):
        article = Path(path)
        return {"article_path": path, "article_sha256": hashlib.sha256(article.read_bytes()).hexdigest(),
                "approval_token": "test-token", "repository": "mock-repository", "remote_url": "https://example.test/mock",
                "branch": "main", "destination": "mock-post.md"}
    monkeypatch.setattr(publishing, "preview", preview)
    monkeypatch.setattr(publishing, "publish", lambda path, **kw: publications.append(kw) or {"status": "pushed", "deployment_status": "unverified"})
    monkeypatch.setattr(config, "TOPIC_DIR", tmp_path / "topics")
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path / "output")
    original_writer = graph.writer
    def writer(state):
        result = original_writer(state)
        result["draft"] += "\n\n本次模拟稿依据：" + state["shared_summary"]
        return result
    monkeypatch.setattr(graph, "writer", writer)

    async def scenario():
        app = WritingApp(tmp_path / "tui")
        async with app.run_test(size=(140, 48)) as pilot:
            async def settled(kind=None):
                for _ in range(200):
                    await pilot.pause(0.02)
                    status = app.controller.status()
                    if status.get("status") == "failed":
                        pytest.fail(status["error"])
                    if not app._action_pending and status.get("status") in {"awaiting_human", "completed"}:
                        app.refresh_status()
                        if kind:
                            assert status["interrupt"]["kind"] == kind
                        return status
                raise AssertionError("终端流程未停在人工边界")
            app.query_one("#topic", Input).value = "终端模拟全流程"
            app.query_one("#message", TextArea).load_text("观点：质量优先，成本次要。")
            app.query_one("#sample", Checkbox).value = True
            await pilot.click("#send")
            first = await settled("summary")
            assert not first["output_path"]
            app.query_one("#message", TextArea).load_text("请面向初学者解释")
            await pilot.click("#send")
            revised = await settled("summary")
            assert revised["summary_version"] > first["summary_version"]
            await pilot.click("#approve")
            await settled("sample")
            assert app.query_one("#approve", Button).disabled
            app.query_one("#message", TextArea).load_text("B")
            await pilot.click("#send")
            final = await settled("final")
            assert not final["output_path"]
            app.query_one("#message", TextArea).load_text("请进一步突出质量优先")
            await pilot.click("#send")
            revised_final = await settled("final")
            assert revised_final["article_version"] > final["article_version"]
            await pilot.click("#approve")
            completed = await settled()
            assert completed["status"] == "completed"
            assert Path(completed["output_path"]).exists()
            assert app.query_one("#publish-confirm", Button).disabled
            assert app.controller.task_id in app._history_ids
            await pilot.click("#publish-preview")
            await settled()
            assert app.controller.publish_preview["article"] == Path(completed["output_path"]).read_text(encoding="utf-8")
            assert not publications
            assert not app.query_one("#publish-confirm", Button).disabled
            app.query_one("#message", TextArea).load_text("尚在考虑")
            await pilot.pause()
            assert app.query_one("#publish-confirm", Button).disabled
            assert app.controller.publish_preview is None
            # Textual 按钮按下后约 0.2 秒内带 -active 状态，期间的点击会被框架忽略；
            # 紧接着再点同一按钮必须等这个状态结束，否则第二次预览根本不会触发。
            for _ in range(100):
                if not app.query_one("#publish-preview", Button).has_class("-active"):
                    break
                await pilot.pause(0.02)
            await pilot.click("#publish-preview")
            await settled()
            # 像用户一样等预览真正显示、按钮可用后再确认。
            for _ in range(200):
                if app.controller.publish_preview is not None and not app.query_one("#publish-confirm", Button).disabled:
                    break
                await pilot.pause(0.02)
            await pilot.click("#publish-confirm")
            for _ in range(200):
                if publications:
                    break
                await pilot.pause(0.02)
            await settled()
            trail = [line.text for line in app.query_one("#chat", RichLog).lines][-6:]
            assert publications == [{"confirmed": True, "approval_token": "test-token"}], (trail, app._action_pending, app.controller.publish_preview is None)
            assert app.query_one("#publish-confirm", Button).disabled
    asyncio.run(scenario())


def test_publish_requires_rendered_preview_and_invalidates_on_selection(tmp_path, monkeypatch):
    from service.tui_controller import TuiController
    from tools import publishing
    import hashlib
    article = tmp_path / "article.md"
    article.write_text("待发布正文", encoding="utf-8")
    plan = {"article_path": str(article), "article_sha256": hashlib.sha256(article.read_bytes()).hexdigest(), "approval_token": "token"}
    called = []
    monkeypatch.setattr(publishing, "preview", lambda path: dict(plan))
    monkeypatch.setattr(publishing, "publish", lambda path, **kw: called.append((path, kw)) or {"status": "pushed"})
    controller = TuiController(tmp_path / "tui")
    controller.manager.tasks["t"] = {"task_id": "t", "thread_id": "t", "status": "completed", "output_path": str(article)}
    controller.select("t")
    preview = controller.preview()
    with pytest.raises(ValueError, match="完整发布预览"):
        controller.publish()
    controller.show_publish_preview(preview)
    controller.select("t")
    with pytest.raises(ValueError, match="完整发布预览"):
        controller.publish()
    controller.show_publish_preview(controller.preview())
    result = controller.publish()
    assert result["status"] == "pushed"
    assert called == [(str(article), {"confirmed": True, "approval_token": "token"})]
    assert controller.publish_preview is None


def test_restart_retry_returns_to_unconfirmed_summary(tmp_path, monkeypatch):
    import config
    from service.tui_controller import TuiController
    monkeypatch.setattr(config, "TOPIC_DIR", tmp_path / "topics")
    controller = TuiController(tmp_path / "tui")
    identity = controller.start("恢复测试", "这是测试输入，等待本人确认")
    controller.manager._threads[identity].join(5)
    assert controller.status()["interrupt"]["kind"] == "summary"
    controller.manager.tasks[identity]["status"] = "interrupted"
    controller.manager._persist(controller.manager.tasks[identity])
    reloaded = TuiController(tmp_path / "tui")
    reloaded.select(identity)
    reloaded.retry()
    reloaded.manager._threads[identity].join(5)
    status = reloaded.status()
    assert status["status"] == "awaiting_human", status["error"]
    assert status["interrupt"]["kind"] == "summary"
    assert "agent1" not in status["progress"] and not status["output_path"]


def test_missing_checkpoint_is_not_reported_as_completed(tmp_path):
    from service import runner
    task = {"task_id": "missing", "thread_id": "missing", "pipeline_version": "v2", "status": "interrupted"}
    runner.continue_task(task, lambda t: None, tmp_path / "empty.sqlite", None)
    assert task["status"] == "failed"
    assert not task.get("output_path")
