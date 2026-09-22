"""真实重写验收：只通过生产服务入口运行；摘要和成稿均不自动确认。"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def validate_approval_versions(decision):
    """批准文件必须自带用户审阅的版本，不从随后状态推测。"""
    if not isinstance(decision, dict):
        raise ValueError("确认文件必须包含JSON对象")
    fields = ("expected_summary_version",) if decision.get("approved") is True else ()
    if decision.get("route") == "approve":
        fields = ("expected_summary_version", "expected_article_version")
    for field in fields:
        if type(decision.get(field)) is not int:
            raise ValueError("确认文件缺少所审阅版本：" + field)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", required=True)
    parser.add_argument("--source")
    parser.add_argument("--topic", help="新任务的文章标题")
    parser.add_argument("--topic-id", default="", help="新任务的主题标识；留空按标题生成")
    parser.add_argument("--resume-decision", help="用户真实确认/反馈的JSON文件；不会自行构造批准")
    parser.add_argument("--continue-run", action="store_true")
    args = parser.parse_args()
    out = Path(args.directory).resolve()
    if not out.is_relative_to(ROOT / "topic"):
        parser.error("验收目录必须在本仓库topic下")
    out.mkdir(parents=True, exist_ok=True)
    os.environ.update(MOCK_LLM="0", MEMORY_ENABLED="0", WRITING_JEV_MODE="off",
                      LLM_CLI_TRACE_DIR=str(out / "cli-calls"), WRITING_HEARTBEAT_FILE=str(out / "heartbeat.json"),
                      WRITING_SOURCE_ARCHIVE_DIR=str(out / "sources"))
    import config
    from service import writing_server as server, runner
    from langgraph.checkpoint.sqlite import SqliteSaver
    server.manager = server.TaskManager(out / "tasks.json", checkpoint_db=out / "checkpoints.sqlite", on_saved=None)
    if args.resume_decision or args.continue_run:
        task_id = (out / "task-id.txt").read_text().strip()
        decision = json.loads(Path(args.resume_decision).read_text(encoding="utf-8")) if args.resume_decision else {}
        if args.resume_decision:
            validate_approval_versions(decision)
        server.writing_resume(task_id, decision)
    else:
        if (out / "task-id.txt").exists():
            parser.error("该验收已有任务，请续跑或指定新目录")
        if not args.source or not args.topic:
            parser.error("新任务需要 --source 与 --topic")
        task_id = server.writing_start_v2(args.topic, Path(args.source).read_text(encoding="utf-8"), args.topic_id)
        (out / "task-id.txt").write_text(task_id)
    (out / "runtime.json").write_text(json.dumps({"roles": config.ROLE_MODELS, "mock": config.MOCK_LLM,
        "entry": "writing_start_v2", "jev_mode": config.JEV_MODE, "memory_enabled": config.MEMORY_ENABLED},
        ensure_ascii=False, indent=2), encoding="utf-8")
    last = None
    while True:
        status = server.writing_status(task_id)
        marker = (status["status"], str(status.get("timeline")))
        if marker != last:
            print(json.dumps({k: status.get(k) for k in ("task_id", "status", "progress", "error")}, ensure_ascii=False), flush=True)
            last = marker
        if status["status"] not in ("pending", "running"):
            break
        time.sleep(2)
    server.manager._threads[task_id].join(timeout=5)
    with SqliteSaver.from_conn_string(str(out / "checkpoints.sqlite")) as saver:
        values = runner.graph_for(server.manager.tasks[task_id], saver).get_state(
            {"configurable": {"thread_id": "svc-" + task_id}}).values
    snapshot = out / ("snapshot-" + time.strftime("%Y%m%d-%H%M%S"))
    snapshot.mkdir()
    for key in ("shared_summary", "outline", "draft", "polished", "review_comments", "final_check_comments"):
        if values.get(key):
            (snapshot / (key + ".md")).write_text(values[key], encoding="utf-8")
            (out / (key + ".md")).write_text(values[key], encoding="utf-8")
    (snapshot / "state.json").write_text(json.dumps(values, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "status.json").write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": status["status"], "kind": (status.get("interrupt") or {}).get("kind"),
                      "snapshot": str(snapshot), "error": status.get("error")}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
