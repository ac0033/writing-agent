"""正常退出只回收本管理器的假CLI，保留断点及预算；不调用真实模型。"""
import asyncio
import json
import os
from pathlib import Path
import sys
import threading
import time
from typing import TypedDict

import pytest

from service.cancellation import CancellationToken, TaskCancelled, current_cancellation


def wait_for(predicate, timeout=8):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("等待测试边界超时")


def pid_alive(pid):
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x100000, False, pid)
        if not handle:
            return False
        try:
            return kernel.WaitForSingleObject(handle, 0) == 258
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        # Linux 已终止但尚未被init回收的僵尸不再执行模型。
        status = Path(f"/proc/{pid}/stat")
        return not (status.exists() and status.read_text().split()[2] == "Z")
    except ProcessLookupError:
        return False


class State(TypedDict, total=False):
    identity: str
    stage: int
    output_path: str


def install_graph(monkeypatch, blocking, after):
    from langgraph.graph import StateGraph, START, END
    from service import runner
    def build(task, checkpointer=None):
        graph = StateGraph(State)
        graph.add_node("prepared", lambda state: {"stage": 1})
        graph.add_node("blocking", blocking)
        graph.add_node("must_not_advance", lambda state: after.append(state["identity"]) or {"stage": 3, "output_path": "not-real.md"})
        graph.add_edge(START, "prepared")
        graph.add_edge("prepared", "blocking")
        graph.add_edge("blocking", "must_not_advance")
        graph.add_edge("must_not_advance", END)
        return graph.compile(checkpointer=checkpointer)
    monkeypatch.setattr(runner, "graph_for", build)
    return build


def manager_for(path, identity):
    from service.writing_server import TaskManager
    path.mkdir(parents=True, exist_ok=True)
    manager = TaskManager(path / "tasks.json", path / "checkpoints.sqlite", on_saved=None)
    task = {"task_id": identity, "thread_id": identity, "pipeline_version": "v2", "status": "pending", "topic": identity}
    manager.tasks[identity] = task
    return manager, task


def fake_tree_script(tmp_path, monkeypatch):
    import agent_cli
    script = tmp_path / "fake_tree.py"
    script.write_text("import os,sys,subprocess,time\nfrom pathlib import Path\n"
        "root=Path(os.environ['PID_DIR']); root.mkdir(parents=True,exist_ok=True)\n"
        "(root/'child.pid').write_text(str(os.getpid()))\n"
        "subprocess.Popen([sys.executable,'-c',\"import os,time;from pathlib import Path;Path(os.environ['PID_DIR'],'grandchild.pid').write_text(str(os.getpid()));time.sleep(60)\"])\n"
        "sys.stdin.read()\ntime.sleep(60)\n", encoding="utf-8")
    monkeypatch.setattr(agent_cli, "command_for", lambda provider: [sys.executable, str(script)])
    return script


def test_shutdown_reaps_child_and_grandchild_only_in_owned_scope(tmp_path, monkeypatch):
    from service import runner
    from langgraph.checkpoint.sqlite import SqliteSaver
    import agent_cli
    fake_tree_script(tmp_path, monkeypatch)
    after = []
    def blocking(state):
        environment = {key: os.environ[key] for key in ("SystemRoot", "TEMP", "PATH") if key in os.environ}
        environment["PID_DIR"] = str(tmp_path / state["identity"] / "pids")
        agent_cli.invoke("codex", "", "fake prompt", timeout=30, environment=environment)
        return {"stage": 2}
    build = install_graph(monkeypatch, blocking, after)
    first, first_task = manager_for(tmp_path / "a", "a")
    second, second_task = manager_for(tmp_path / "b", "b")
    try:
        first._spawn("a", runner.drive, first_task, {"identity": "a"})
        second._spawn("b", runner.drive, second_task, {"identity": "b"})
        wait_for(lambda: all((tmp_path / name / "pids" / "grandchild.pid").exists() for name in ("a", "b")))
        pids = {name: [int((tmp_path / name / "pids" / filename).read_text()) for filename in ("child.pid", "grandchild.pid")] for name in ("a", "b")}
        result = first.shutdown(timeout=8)
        assert result == {"remaining_tasks": [], "remaining_cli": []}
        assert not any(pid_alive(pid) for pid in pids["a"])
        assert all(pid_alive(pid) for pid in pids["b"])
        assert first_task["status"] == "interrupted" and second_task["status"] == "running"
        assert not after
        persisted = json.loads((tmp_path / "a" / "tasks.json").read_text(encoding="utf-8"))["a"]
        assert persisted["model_calls"] == 1 and persisted["model_seconds"] > 0
        with SqliteSaver.from_conn_string(str(first.checkpoint_db)) as saver:
            state = build(first_task, saver).get_state({"configurable": {"thread_id": "a"}})
            assert state.values["stage"] == 1
            assert "blocking" in state.next
        with pytest.raises(ValueError, match="正在退出"):
            first.resume("a", {})
    finally:
        first.shutdown(timeout=8)
        second.shutdown(timeout=8)


def test_api_wait_shutdown_is_bounded_settles_time_and_drops_late_return(tmp_path, monkeypatch):
    from service import runner
    from service.model_budget import model_request
    entered, release = threading.Event(), threading.Event()
    after = []
    def blocking(state):
        with model_request("deepseek"):
            entered.set()
            assert release.wait(10)
        return {"stage": 2}
    install_graph(monkeypatch, blocking, after)
    manager, task = manager_for(tmp_path, "api")
    try:
        manager._spawn("api", runner.drive, task, {"identity": "api"})
        assert entered.wait(5)
        time.sleep(0.04)
        started = time.monotonic()
        result = manager.shutdown(timeout=0.05)
        assert time.monotonic() - started < 1
        assert result["remaining_cli"] == [] and result["remaining_tasks"] == ["api"]
        assert task["status"] == "interrupted" and task["model_calls"] == 1
        booked = task["model_seconds"]
        assert booked > 0
        manager.shutdown(timeout=0)
        # 模拟退出释放目录锁后，新TUI打开同一目录；旧API稍后返回不能覆盖新登记。
        from service.writing_server import TaskManager
        replacement = TaskManager(tmp_path / "tasks.json", manager.checkpoint_db, on_saved=None)
        replacement.tasks["new-task"] = {"task_id": "new-task", "thread_id": "new-task", "status": "awaiting_human"}
        replacement._persist(replacement.tasks["new-task"])
        disk_before = (tmp_path / "tasks.json").read_bytes()
        release.set()
        manager._threads["api"].join(5)
        assert not manager._threads["api"].is_alive()
        assert not after and task["status"] == "interrupted"
        assert booked <= task["model_seconds"] < 1
        assert (tmp_path / "tasks.json").read_bytes() == disk_before
    finally:
        release.set()
        manager.shutdown(timeout=5)


def test_cancel_before_start_prevents_process_launch(monkeypatch):
    import agent_cli
    token = CancellationToken()
    token.cancel()
    scope = current_cancellation.set(token)
    monkeypatch.setattr(agent_cli.subprocess, "Popen", lambda *a, **kw: pytest.fail("取消后不能启动进程"))
    try:
        with pytest.raises(TaskCancelled):
            agent_cli._stream_process(["fake"], input="", cwd=None, timeout=1, env={}, progress=agent_cli._EventProgress("codex", None))
    finally:
        current_cancellation.reset(scope)


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object")
def test_bound_job_reaps_descendants_when_own_fake_host_is_killed(tmp_path, monkeypatch):
    import subprocess
    fake = fake_tree_script(tmp_path, monkeypatch)
    host = tmp_path / "fake_host.py"
    host.write_text("import sys,subprocess,time\nfrom pathlib import Path\n"
        "from service.process_job import bind_process\n"
        "proc=subprocess.Popen([sys.executable,sys.argv[1]],stdin=subprocess.PIPE)\n"
        "close_job=bind_process(proc)\n"
        "proc.stdin.close()\n"
        "Path(sys.argv[2]).write_text('bound')\n"
        "time.sleep(60)\n", encoding="utf-8")
    environment = {key: os.environ[key] for key in ("SystemRoot", "TEMP", "PATH") if key in os.environ}
    environment.update(PID_DIR=str(tmp_path / "pids"), PYTHONPATH=str(Path.cwd()))
    proc = subprocess.Popen([sys._base_executable, str(host), str(fake), str(tmp_path / "bound")], env=environment)
    try:
        # 三层解释器冷启动不计入退出时限；先等确实启动，再强制结束假宿主。
        wait_for(lambda: (tmp_path / "bound").exists() and (tmp_path / "pids" / "grandchild.pid").exists(), timeout=20)
        pids = [int((tmp_path / "pids" / name).read_text()) for name in ("child.pid", "grandchild.pid")]
        assert all(pid_alive(pid) for pid in pids)
        proc.kill()  # 仅终止此测试刚创建的假宿主，不执行全局进程查杀。
        proc.wait(5)
        wait_for(lambda: not any(pid_alive(pid) for pid in pids))
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(5)


def test_tui_quit_waits_for_its_fake_cli_before_releasing_directory(tmp_path, monkeypatch):
    import agent_cli
    from service import runner
    from service.tui_controller import TuiController
    from tui import WritingApp
    fake_tree_script(tmp_path, monkeypatch)
    after = []
    def blocking(state):
        environment = {key: os.environ[key] for key in ("SystemRoot", "TEMP", "PATH") if key in os.environ}
        environment["PID_DIR"] = str(tmp_path / "pids")
        agent_cli.invoke("codex", "", "fake", timeout=30, environment=environment)
        return {"stage": 2}
    install_graph(monkeypatch, blocking, after)
    controller = TuiController(tmp_path / "ui")
    task = {"task_id": "ui-test", "thread_id": "ui-test", "pipeline_version": "v2", "status": "pending", "topic": "fake"}
    controller.manager.tasks["ui-test"] = task
    controller.select("ui-test")
    async def scenario():
        app = WritingApp(tmp_path / "ui", controller=controller)
        async with app.run_test(size=(120, 40)) as pilot:
            controller.manager._spawn("ui-test", runner.drive, task, {"identity": "ui"})
            for _ in range(200):
                await pilot.pause(0.02)
                if (tmp_path / "pids" / "grandchild.pid").exists():
                    break
            assert (tmp_path / "pids" / "grandchild.pid").exists()
            await pilot.press("ctrl+q")
        assert task["status"] == "interrupted"
        assert not controller.manager._threads["ui-test"].is_alive()
        assert app._directory_guard is None
    try:
        asyncio.run(scenario())
        for filename in ("child.pid", "grandchild.pid"):
            assert not pid_alive(int((tmp_path / "pids" / filename).read_text()))
        assert not after
    finally:
        controller.manager.shutdown(timeout=8)

