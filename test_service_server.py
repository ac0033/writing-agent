"""service/writing_server.py 的测试：TaskManager + 四个工具的端到端行为。

全程 MOCK_LLM 模式（conftest.py 已设环境变量），后台线程跑图但都是 mock
节点，秒级完成。tasks.json / 检查点库 / 成稿目录全部指向 tmp_path，
on_saved 注入空操作，不在写作仓库里产生 git commit。
"""
import json
import time
from pathlib import Path

import pytest

import config
from service.writing_server import TaskManager


@pytest.fixture
def mgr(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path / "output")
    return TaskManager(tasks_path=tmp_path / "tasks.json",
                       checkpoint_db=tmp_path / "checkpoints.sqlite",
                       on_saved=lambda task: None)


def _wait(mgr: TaskManager, task_id: str, pred, timeout: float = 60) -> dict:
    """轮询直到 status 满足 pred 或任务失败/超时。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = mgr.status(task_id)
        if pred(s) or s["status"] == "failed":
            return s
        time.sleep(0.2)
    raise AssertionError(f"超时：任务未满足条件，当前 {mgr.status(task_id)}")


def test_full_flow_persist_and_reload(mgr):
    """自动模式：start 立即返回 task_id → 后台跑到 completed →
    result 给出成稿目录和 article.md → tasks.json 落盘且重启后（新建
    TaskManager）旧任务状态还在。"""
    tid = mgr.start("MCP 集成测试", idea="", auto_approve=True)
    assert isinstance(tid, str) and tid

    s = _wait(mgr, tid, lambda x: x["status"] == "completed")
    assert s["status"] == "completed", s.get("error")
    assert s["thread_id"] == f"svc-{tid}"
    assert "agent5" in s["progress"]

    r = mgr.result(tid)
    assert r["status"] == "completed"
    assert Path(r["output_dir"]).is_dir()
    assert "润色稿（mock）" in r["article"]

    # tasks.json 持久化
    data = json.loads(mgr.tasks_path.read_text(encoding="utf-8"))
    assert tid in data and data[tid]["status"] == "completed"

    # 模拟 server 重启：新建 TaskManager 读同一个 tasks.json
    mgr2 = TaskManager(tasks_path=mgr.tasks_path,
                       checkpoint_db=mgr.checkpoint_db,
                       on_saved=lambda task: None)
    assert mgr2.status(tid)["status"] == "completed"
    assert "润色稿（mock）" in mgr2.result(tid)["article"]


def test_suspend_and_resume_via_manager(mgr):
    """人工模式：挂在 human_outline（payload 里有大纲）→ resume 通过 →
    挂在 human_final（payload 里有成稿）→ resume 批准 → completed。"""
    tid = mgr.start("挂起测试", auto_approve=False)

    s = _wait(mgr, tid, lambda x: x["status"] == "awaiting_human")
    assert s["awaiting_human"] is True
    assert s["interrupt"]["kind"] == "outline"
    assert "大纲（mock）" in s["interrupt"]["outline"]
    # 未完成时 result 给状态说明而不是稿子
    assert "detail" in mgr.result(tid)

    msg = mgr.resume(tid, {"approved": True, "feedback": ""})
    assert tid in msg
    s = _wait(mgr, tid, lambda x: x["status"] == "awaiting_human"
              and (x["interrupt"] or {}).get("kind") == "final")
    assert "润色稿（mock）" in s["interrupt"]["polished"]

    mgr.resume(tid, {"route": "approve", "feedback": ""})
    s = _wait(mgr, tid, lambda x: x["status"] == "completed")
    assert s["status"] == "completed", s.get("error")
    assert "润色稿（mock）" in mgr.result(tid)["article"]


def test_resume_rejects_bad_state_and_bad_decision(mgr):
    """resume 的入参校验：未知任务、未挂起的任务、格式不对的 decision。"""
    with pytest.raises(ValueError, match="未知任务"):
        mgr.resume("no-such-task", {})

    tid = mgr.start("校验测试", auto_approve=False)
    _wait(mgr, tid, lambda x: x["status"] == "awaiting_human")

    # outline 确认点缺 approved 字段：当场报错，不动任务状态
    with pytest.raises(ValueError, match="approved"):
        mgr.resume(tid, {"route": "approve"})
    assert mgr.status(tid)["status"] == "awaiting_human"

    # 正常走完，完成后再 resume 应被拒绝
    mgr.resume(tid, {"approved": True, "feedback": ""})
    _wait(mgr, tid, lambda x: x["status"] == "awaiting_human"
          and (x["interrupt"] or {}).get("kind") == "final")
    mgr.resume(tid, {"route": "approve", "feedback": ""})
    _wait(mgr, tid, lambda x: x["status"] == "completed")
    with pytest.raises(ValueError, match="不能 resume"):
        mgr.resume(tid, {"route": "approve", "feedback": ""})


def test_running_task_marked_interrupted_on_reload(tmp_path, monkeypatch):
    """server 重启时 status=running 的任务降级为 interrupted（可续跑）。"""
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path / "output")
    tasks_path = tmp_path / "tasks.json"
    tasks_path.write_text(json.dumps({
        "t1": {"task_id": "t1", "thread_id": "svc-t1", "topic": "x",
               "status": "running"},
        "t2": {"task_id": "t2", "thread_id": "svc-t2", "topic": "y",
               "status": "awaiting_human"},
    }, ensure_ascii=False), encoding="utf-8")

    mgr = TaskManager(tasks_path=tasks_path,
                      checkpoint_db=tmp_path / "checkpoints.sqlite",
                      on_saved=lambda task: None)
    assert mgr.status("t1")["status"] == "interrupted"
    assert mgr.status("t2")["status"] == "awaiting_human"  # 挂起的不动
