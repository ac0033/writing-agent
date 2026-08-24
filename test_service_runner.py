"""service/runner.py 的测试：图的自动驱动循环。

全程 MOCK_LLM 模式（conftest.py 已设环境变量），不发真实 LLM 请求、
不接记忆服务；检查点库和成稿目录都指到 tmp_path，不碰真实文件。
"""
from pathlib import Path

import pytest

import config
from service import runner


@pytest.fixture
def env(tmp_path, monkeypatch):
    """隔离环境：成稿写临时目录，检查点用临时 sqlite，持久化回调只记账。"""
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path / "output")
    db = tmp_path / "checkpoints.sqlite"
    persisted: list[str] = []

    def persist(task: dict) -> None:
        persisted.append(task["status"])

    return db, persist, persisted


def _task(thread_id: str, auto_approve: bool) -> dict:
    return {"thread_id": thread_id, "topic": "测试主题", "idea": "",
            "auto_approve": auto_approve, "status": "pending", "interrupt": None}


def test_auto_approve_runs_to_save(env):
    """auto_approve=True：从头跑到 save，两个人工确认点自动通过。"""
    db, persist, persisted = env
    task = _task("t-auto", auto_approve=True)

    runner.start_task(task, persist, checkpoint_db=db, on_saved=None)

    assert task["status"] == "completed"
    assert task["interrupt"] is None
    # 成稿真的落盘了，内容是 mock 润色稿
    article = Path(task["output_path"])
    assert article.exists()
    assert "润色稿（mock）" in article.read_text(encoding="utf-8")
    # 五个 LLM 节点都跑过了（进度来自 thinking_log）
    for node in ("agent1", "agent3", "agent2", "agent4", "agent5"):
        assert node in task["progress"]
    # 状态变化都通知过 persist
    assert "running" in persisted and "completed" in persisted


def test_manual_suspend_and_resume(env):
    """auto_approve=False：在 human_outline 挂起，resume 后走到 human_final，
    再 resume 才完成。验证 interrupt payload 的内容就是大纲/成稿。"""
    db, persist, _ = env
    task = _task("t-manual", auto_approve=False)

    # 第一程：跑到 human_outline 挂起
    runner.start_task(task, persist, checkpoint_db=db, on_saved=None)
    assert task["status"] == "awaiting_human"
    assert task["interrupt"]["kind"] == "outline"
    assert "大纲（mock）" in task["interrupt"]["outline"]

    # 通过大纲 → 跑到 human_final 挂起
    runner.resume_task(task, {"approved": True, "feedback": ""},
                       persist, checkpoint_db=db, on_saved=None)
    assert task["status"] == "awaiting_human"
    assert task["interrupt"]["kind"] == "final"
    assert "润色稿（mock）" in task["interrupt"]["polished"]

    # 终审通过 → 跑到 save 完成
    runner.resume_task(task, {"route": "approve", "feedback": ""},
                       persist, checkpoint_db=db, on_saved=None)
    assert task["status"] == "completed"
    assert Path(task["output_path"]).exists()


def test_outline_rejection_loops_back(env):
    """大纲被打回（approved=False）：回到 architect 重出大纲，再次挂起。"""
    db, persist, _ = env
    task = _task("t-reject", auto_approve=False)

    runner.start_task(task, persist, checkpoint_db=db, on_saved=None)
    assert task["interrupt"]["kind"] == "outline"

    runner.resume_task(task, {"approved": False, "feedback": "重写大纲"},
                       persist, checkpoint_db=db, on_saved=None)
    # 重出一版大纲后应再次挂在 human_outline
    assert task["status"] == "awaiting_human"
    assert task["interrupt"]["kind"] == "outline"


def test_on_saved_called_after_completion(env):
    """成稿落盘后触发 on_saved 回调（server 用它做 git 快照），
    返回值记进 task；回调抛错不影响 completed 状态。"""
    db, persist, _ = env
    task = _task("t-snap", auto_approve=True)

    runner.start_task(task, persist, checkpoint_db=db,
                      on_saved=lambda t: "fake-commit-hash")
    assert task["status"] == "completed"
    assert task["snapshot_commit"] == "fake-commit-hash"

    def boom(t):
        raise RuntimeError("git 不可用")

    task2 = _task("t-snap-fail", auto_approve=True)
    runner.start_task(task2, persist, checkpoint_db=db, on_saved=boom)
    assert task2["status"] == "completed"  # 快照失败不拖垮成稿
    assert "git 不可用" in task2["snapshot_error"]


def test_timeline_records_node_events(env):
    """stream 驱动逐节点记 timeline：覆盖主链路节点，每条带时间戳和非负耗时。"""
    db, persist, _ = env
    task = _task("t-timeline", auto_approve=True)

    runner.start_task(task, persist, checkpoint_db=db, on_saved=None)

    assert task["status"] == "completed"
    nodes = [e["node"] for e in task["timeline"]]
    # 主链路的关键节点都应有事件（human_* 节点只 interrupt 不产 update，不在列）
    for node in ("architect", "researcher", "writer", "reviewer", "stylist", "save"):
        assert node in nodes
    for e in task["timeline"]:
        assert e["at"] and e["dur_s"] >= 0
