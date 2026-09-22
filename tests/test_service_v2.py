"""真实 TaskManager / SQLite / mock 节点集成；不发请求、不创建 Git 快照。"""
import json
import threading
from pathlib import Path

import pytest

import config
import main
from service import runner, writing_server as server
from tools import topics


@pytest.fixture
def mgr(tmp_path, monkeypatch):
    assert config.MOCK_LLM
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path / "output")
    monkeypatch.setattr(config, "TOPIC_DIR", tmp_path / "topic")
    monkeypatch.setattr(config, "GRAPH_RECURSION_LIMIT", 150)
    return server.TaskManager(tmp_path / "tasks.json", tmp_path / "checkpoints.sqlite", on_saved=None)


def settled(mgr, tid):
    mgr._threads[tid].join(timeout=20)
    assert not mgr._threads[tid].is_alive(), "mock 运行未按时结束"
    state = mgr.status(tid)
    assert state["status"] != "failed", state["error"]
    return state


def checkpoint(mgr, tid):
    from langgraph.checkpoint.sqlite import SqliteSaver
    with SqliteSaver.from_conn_string(str(mgr.checkpoint_db)) as saver:
        graph = runner.graph_for(mgr.tasks[tid], saver)
        return dict(graph.get_state({"configurable": {"thread_id": mgr.tasks[tid]["thread_id"]}}).values)


def approval(mgr, tid):
    payload = mgr.status(tid)["interrupt"]
    if payload["kind"] == "summary":
        return {"approved": True, "expected_summary_version": payload["summary_version"]}
    return {"route": "approve", "expected_summary_version": payload["summary_version"],
            "expected_article_version": payload["article_version"]}


def test_v2_real_manager_summary_revision_final_feedback_and_save(mgr, monkeypatch):
    import graph
    original_writer = graph.writer
    def writer(state):
        result = original_writer(state)
        if state.get("revision_pending"):
            result["draft"] += "\n\n修改后的mock正文：" + state["shared_summary"]
        return result
    monkeypatch.setattr(graph, "writer", writer)
    tid = mgr.start("vibe coding 测试", "观点：质量优先", auto_approve=True, pipeline_version="v2")
    state = settled(mgr, tid)
    assert state["interrupt"]["kind"] == "summary"
    assert "agent1" not in state["progress"]
    mgr.resume(tid, {"approved": False, "feedback": "成本只作为次要因素"})
    state = settled(mgr, tid)
    assert state["interrupt"]["kind"] == "summary"
    assert "成本只作为次要因素" in state["interrupt"]["summary"]
    mgr.resume(tid, approval(mgr, tid))
    state = settled(mgr, tid)
    assert state["interrupt"]["kind"] == "final"
    first_version = state["interrupt"]["article_version"]
    assert not state["output_path"]
    assert [row["node"] for row in state["timeline"]].count("reviewer") >= 2
    mgr.resume(tid, {"route": "feedback", "feedback": "请把质量优先放在开头"})
    state = settled(mgr, tid)
    assert state["interrupt"]["kind"] == "final"
    assert state["interrupt"]["article_version"] > first_version
    assert "质量优先放在开头" in state["interrupt"]["summary"]
    mgr.resume(tid, approval(mgr, tid))
    state = settled(mgr, tid)
    assert state["status"] == "completed"
    article = Path(state["output_path"])
    assert article.exists()
    assert "质量优先放在开头" in (article.parent / "shared_summary.md").read_text(encoding="utf-8")
    assert json.loads((article.parent / "pipeline_v2.json").read_text(encoding="utf-8"))["pipeline_version"] == "v2"
    assert checkpoint(mgr, tid)["user_idea"] == "观点：质量优先"
    assert mgr.result(tid)["publication"]["status"] == "not_published"


def test_v2_restart_preserves_summary_confirmation(mgr):
    tid = mgr.start("重启测试", "作者原话", auto_approve=True, pipeline_version="v2")
    assert settled(mgr, tid)["interrupt"]["kind"] == "summary"
    reloaded = server.TaskManager(mgr.tasks_path, mgr.checkpoint_db, on_saved=None)
    assert reloaded.status(tid)["pipeline_version"] == "v2"
    reloaded.resume(tid, approval(reloaded, tid))
    assert settled(reloaded, tid)["interrupt"]["kind"] == "final"


def test_legacy_missing_version_keeps_old_graph(mgr, monkeypatch):
    monkeypatch.setattr(config, "PIPELINE_VERSION", "v2")
    tid = mgr.start("旧任务", "旧输入", auto_approve=False)
    assert settled(mgr, tid)["interrupt"]["kind"] == "outline"
    mgr.tasks[tid].pop("pipeline_version")
    mgr._persist(mgr.tasks[tid])
    reloaded = server.TaskManager(mgr.tasks_path, mgr.checkpoint_db, on_saved=None)
    assert reloaded.status(tid)["pipeline_version"] == "v1"
    reloaded.resume(tid, {"approved": True})
    assert settled(reloaded, tid)["interrupt"]["kind"] == "final"
    assert "summary" not in reloaded.status(tid)["progress"]


def test_source_entry_saves_original_and_starts_summary(mgr, monkeypatch):
    monkeypatch.setattr(server, "manager", mgr)
    original = "第一条观点。\n\n这是仍在探索的想法，不要当成结论。"
    tid = server.writing_start_v2("原材料入口", original)
    state = settled(mgr, tid)
    assert state["pipeline_version"] == "v2" and state["interrupt"]["kind"] == "summary"
    saved = topics.load(state["topic_file"])
    assert saved["schema"] == "writing-source-v2"
    assert original in saved["idea"]
    assert saved["topic_sha256"] == state["topic_sha256"]
    second = topics.prepare_source("原材料入口", "后来补充的想法")
    assert second["topic_file"] != state["topic_file"]
    assert original in Path(state["topic_file"]).read_text(encoding="utf-8")
    assert original in checkpoint(mgr, tid)["user_idea"]


def test_legacy_start_with_source_file_uses_v2(mgr, monkeypatch):
    monkeypatch.setattr(server, "manager", mgr)
    saved = topics.prepare_source("转入口", "作者原话")
    tid = server.writing_start(topic_file=saved["topic_file"], auto_approve=True)
    assert settled(mgr, tid)["interrupt"]["kind"] == "summary"


@pytest.mark.parametrize("kind,answer,expected", [
    ("summary", "确认", {"approved": True, "feedback": ""}),
    ("summary", "重点是质量", {"approved": False, "feedback": "重点是质量"}),
    ("final", "确认", {"route": "approve", "feedback": ""}),
    ("final", "这一段太绝对", {"route": "feedback", "feedback": "这一段太绝对"}),
    ("decision", "保留原立场", {"route": "feedback", "feedback": "保留原立场"}),
])
def test_cli_v2_payload(kind, answer, expected, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _: answer)
    payload = {"kind": kind, "pipeline_version": "v2", "summary": "原意", "polished": "正文",
               "summary_version": 3, "article_version": 5}
    if kind in {"summary", "final"}:
        expected["expected_summary_version"] = 3
    if kind == "final":
        expected["expected_article_version"] = 5
    assert main.handle_interrupt(payload) == expected


def test_service_rejects_wrong_confirmation_shape(mgr):
    tid = mgr.start("校验", "原意", pipeline_version="v2")
    settled(mgr, tid)
    with pytest.raises(ValueError):
        mgr.resume(tid, {"route": "approve"})
    assert mgr.status(tid)["interrupt"]["kind"] == "summary"
    mgr.resume(tid, approval(mgr, tid))
    settled(mgr, tid)
    with pytest.raises(ValueError):
        mgr.resume(tid, {"route": "style", "feedback": "修改"})
    assert mgr.status(tid)["interrupt"]["kind"] == "final"


def test_cli_source_cannot_downgrade_to_v1(mgr, monkeypatch, tmp_path):
    saved = topics.prepare_source("新原材料", "原始观点")
    monkeypatch.setattr(config, "CHECKPOINT_DB", tmp_path / "cli.sqlite")
    monkeypatch.setattr(config, "SESSIONS_FILE", tmp_path / "sessions.json")
    monkeypatch.setattr("sys.argv", ["main.py", "--topic-file", saved["topic_file"], "--pipeline", "v1"])
    monkeypatch.setattr(main, "build_graph", lambda **kw: pytest.fail("v2原材料不得进入旧图"))
    with pytest.raises(SystemExit) as exc:
        main.main()
    assert exc.value.code == 2


def test_v2_approval_requires_displayed_versions(mgr):
    tid = mgr.start("版本校验", "原意", pipeline_version="v2")
    settled(mgr, tid)
    with pytest.raises(ValueError, match="所审阅版本"):
        mgr.resume(tid, {"approved": True})
    with pytest.raises(ValueError, match="版本已经变化"):
        mgr.resume(tid, {"approved": True, "expected_summary_version": 0})
    mgr.resume(tid, approval(mgr, tid))
    settled(mgr, tid)
    current = approval(mgr, tid)
    stale = {**current, "expected_article_version": current["expected_article_version"] - 1}
    with pytest.raises(ValueError, match="版本已经变化"):
        mgr.resume(tid, stale)
    with pytest.raises(ValueError, match="所审阅版本"):
        mgr.resume(tid, {"route": "approve"})
    assert mgr.status(tid)["status"] == "awaiting_human"


def test_live_feedback_does_not_wait_for_model(mgr, monkeypatch):
    import graph
    entered, release = threading.Event(), threading.Event()
    original_writer = graph.writer
    def slow_writer(state):
        if not entered.is_set():
            entered.set()
            assert release.wait(10)
        result = original_writer(state)
        result["draft"] += "\nmock修订：" + state["shared_summary"]
        return result
    monkeypatch.setattr(graph, "writer", slow_writer)
    tid = mgr.start("运行中反馈", "原意", pipeline_version="v2")
    settled(mgr, tid)
    mgr.resume(tid, approval(mgr, tid))
    assert entered.wait(10)
    responses = []
    update = threading.Thread(target=lambda: responses.append(mgr.update_input(tid, "强调新手可读性")))
    update.start()
    try:
        update.join(1)
        assert not update.is_alive(), "模型执行不应持有反馈队列锁"
        assert responses[0]["status"] == "queued"
    finally:
        release.set()
        update.join(5)
    state = settled(mgr, tid)
    assert state["interrupt"]["kind"] == "final"
    assert "强调新手可读性" in state["interrupt"]["summary"]
    assert state["pending_feedback_count"] == 0


def test_feedback_after_save_gate_is_rejected_without_waiting_for_io(mgr, monkeypatch):
    import graph
    entered, release = threading.Event(), threading.Event()
    original_save = graph.save
    def slow_save(state):
        entered.set()
        assert release.wait(10)
        return original_save(state)
    monkeypatch.setattr(graph, "save", slow_save)
    tid = mgr.start("保存期间反馈", "原意", pipeline_version="v2")
    settled(mgr, tid)
    mgr.resume(tid, approval(mgr, tid))
    settled(mgr, tid)
    mgr.resume(tid, approval(mgr, tid))
    assert entered.wait(10)
    rejected = []
    def update():
        try:
            mgr.update_input(tid, "晚到反馈")
        except ValueError as error:
            rejected.append(str(error))
    worker = threading.Thread(target=update)
    worker.start()
    try:
        worker.join(1)
        assert not worker.is_alive(), "保存IO不应持有反馈队列锁"
        assert rejected and "未接收本次更新" in rejected[0]
        assert mgr.status(tid)["pending_feedback_count"] == 0
    finally:
        release.set()
        worker.join(5)
    assert settled(mgr, tid)["status"] == "completed"


def test_feedback_before_save_gate_invalidates_approval(mgr, monkeypatch):
    import graph
    import pipeline_v2 as pipeline
    entered, release = threading.Event(), threading.Event()
    original_save, original_writer = pipeline.save, graph.writer
    def before_gate(state):
        entered.set()
        assert release.wait(10)
        return original_save(state)
    def writer(state):
        result = original_writer(state)
        result["draft"] += "\nmock修订：" + state["shared_summary"]
        return result
    monkeypatch.setattr(pipeline, "save", before_gate)
    monkeypatch.setattr(graph, "writer", writer)
    tid = mgr.start("保存前反馈", "原意", pipeline_version="v2")
    settled(mgr, tid)
    mgr.resume(tid, approval(mgr, tid))
    settled(mgr, tid)
    mgr.resume(tid, approval(mgr, tid))
    assert entered.wait(10)
    try:
        assert mgr.update_input(tid, "保存前更正重点")["status"] == "queued"
    finally:
        release.set()
    state = settled(mgr, tid)
    assert state["status"] == "awaiting_human"
    assert state["interrupt"]["kind"] == "final"
    assert "保存前更正重点" in state["interrupt"]["summary"]
    assert not state["output_path"] and not state["save_started"]


def test_accept_script_requires_explicit_version_file():
    from scripts.accept_v2 import validate_approval_versions
    with pytest.raises(ValueError, match="版本"):
        validate_approval_versions({"approved": True})
    with pytest.raises(ValueError, match="版本"):
        validate_approval_versions({"route": "approve", "expected_summary_version": 1})
    validate_approval_versions({"route": "approve", "expected_summary_version": 1, "expected_article_version": 2})
