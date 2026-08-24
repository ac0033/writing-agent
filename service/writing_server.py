"""FastMCP server（stdio）：把写作工作流暴露成 MCP 工具。

工具：
- writing_start(topic, idea="", auto_approve=True) -> task_id
  后台线程跑图，立即返回 task_id。初始 state 的组装照抄 main.py 的启动逻辑。
- writing_status(task_id) -> 节点进度 / 是否挂在人工确认点 / 挂起时的
  interrupt payload（大纲或待确认成稿）。
- writing_resume(task_id, decision) -> 挂起任务传入 resume 值继续跑（后台线程，
  立即返回）。decision 格式与 main.py handle_interrupt 的返回一致：
  outline 确认点 {"approved": bool, "feedback": str}；
  final 确认点 {"route": "approve"|"content"|"style", "feedback": str}。
- writing_result(task_id) -> output/ 下成稿目录路径与 article.md 内容；
  未完成时返回当前状态说明。

任务状态持久化到 service/tasks.json（TaskManager 负责），server 重启后
旧任务可查；挂在确认点（awaiting_human）的任务可直接 resume 续跑（存档在
sqlite 检查点里），重启瞬间正在跑的（running）任务降级为 interrupted，
用 writing_resume 从上一个检查点继续。

成稿落盘（图跑到 save 节点完成）后自动 git 快照（service/snapshot.py）。

运行方式（cwd 必须是仓库根，保证 config/graph 可导入）：
    uv run python -m service.writing_server
MEMORY_ENABLED=0 uv run python -m service.writing_server   # 关闭记忆副作用
"""
import json
import os
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from mcp.server.fastmcp import FastMCP

import config
from service import runner, snapshot

TASKS_FILE = Path(__file__).parent / "tasks.json"
HEARTBEAT_FILE = Path(__file__).parent / "heartbeat.json"

# 节点内 LLM 调用的活性心跳落在这里（log.py 的 heartbeat() 读取该环境变量）；
# import 时即设置，保证 runner 线程里的图执行也能看到。
os.environ.setdefault("WRITING_HEARTBEAT_FILE", str(HEARTBEAT_FILE))

_FINAL_ROUTES = ("approve", "content", "style")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def default_on_saved(task: dict) -> str | None:
    """成稿落盘后的默认动作：对写作仓库做 git 快照。"""
    return snapshot.git_snapshot(config.BASE_DIR, f"post: {task.get('topic', '?')}")


class TaskManager:
    """任务登记簿：内存 dict + tasks.json 持久化 + 后台线程跑 runner。

    checkpoint_db / on_saved 可注入，测试时用临时路径和空快照避免污染
    真实检查点库和真实 git 历史；生产默认 config.CHECKPOINT_DB + git 快照。
    """

    def __init__(self, tasks_path: Path = TASKS_FILE, checkpoint_db=None,
                 on_saved=default_on_saved):
        self.tasks_path = Path(tasks_path)
        self.checkpoint_db = checkpoint_db
        self.on_saved = on_saved
        self._lock = threading.Lock()
        self._threads: dict[str, threading.Thread] = {}
        self.tasks: dict[str, dict] = self._load()

    # ---- 持久化 ----

    def _load(self) -> dict:
        if not self.tasks_path.exists():
            return {}
        tasks = json.loads(self.tasks_path.read_text(encoding="utf-8"))
        for t in tasks.values():
            # 上次进程退出时正在跑的任务：图的中断点仍在 sqlite 检查点里，
            # 标 interrupted，可用 resume() 从上一个检查点继续。
            if t.get("status") in ("pending", "running"):
                t["status"] = "interrupted"
        return tasks

    def _persist(self, task: dict) -> None:
        task["updated_at"] = _now()
        with self._lock:
            # 原子写：先落临时文件再替换，避免写一半进程被杀留下坏 json
            tmp = self.tasks_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.tasks, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            tmp.replace(self.tasks_path)

    # ---- 工具实现 ----

    def start(self, topic: str, idea: str = "", auto_approve: bool = True) -> str:
        task_id = uuid.uuid4().hex[:8]
        task = {
            "task_id": task_id,
            "thread_id": f"svc-{task_id}",  # 与 CLI 会话的 thread_id 区分，互不串场
            "topic": topic,
            "idea": idea,
            "auto_approve": bool(auto_approve),
            "status": "pending",   # pending/running/awaiting_human/completed/failed/interrupted
            "interrupt": None,
            "progress": [],
            "next_nodes": [],
            "output_path": "",
            "error": "",
            "created_at": _now(),
            "updated_at": _now(),
        }
        with self._lock:
            self.tasks[task_id] = task
        self._persist(task)
        self._spawn(task_id, runner.start_task, task)
        return task_id

    def status(self, task_id: str) -> dict:
        task = self.tasks.get(task_id)
        if task is None:
            return {"error": f"未知任务：{task_id}"}
        awaiting = task["status"] == "awaiting_human"
        return {
            "task_id": task_id,
            "thread_id": task["thread_id"],
            "topic": task.get("topic", ""),
            "status": task["status"],
            "progress": task.get("progress", []),
            "next_nodes": task.get("next_nodes", []),
            # 节点级时间线：[{node, at, dur_s}]，实时反映跑到哪、每步多久
            "timeline": task.get("timeline", []),
            # 节点内 LLM 调用的活性心跳：判断"在生成"还是"卡死"的依据
            "heartbeat": self._read_heartbeat(),
            "awaiting_human": awaiting,
            # 挂起时把 payload（大纲/成稿）带出来，供外部审阅后决定 resume 值
            "interrupt": task.get("interrupt") if awaiting else None,
            "output_path": task.get("output_path", ""),
            "error": task.get("error", ""),
            "created_at": task.get("created_at", ""),
            "updated_at": task.get("updated_at", ""),
        }

    @staticmethod
    def _read_heartbeat() -> dict | None:
        """读心跳文件并补一个 age_s（距现在几秒）。读不到就返回 None。"""
        try:
            data = json.loads(HEARTBEAT_FILE.read_text(encoding="utf-8"))
            data["age_s"] = round(time.time() - data.get("ts", 0), 1)
            return data
        except (OSError, json.JSONDecodeError):
            return None

    def resume(self, task_id: str, decision: dict) -> str:
        task = self.tasks.get(task_id)
        if task is None:
            raise ValueError(f"未知任务：{task_id}")
        status = task["status"]
        if status == "awaiting_human":
            self._validate_decision(task, decision)
            self._spawn(task_id, runner.resume_task, task, decision)
            return f"任务 {task_id} 已用传入的 decision 继续执行（后台）。"
        if status in ("interrupted", "failed"):
            # 检查点还在 sqlite 里：从上一个节点边界继续，已完成节点不重跑。
            # failed 常见于 LLM 硬错误（余额/key/网络），修复后从这里续跑。
            self._spawn(task_id, runner.continue_task, task)
            return f"任务 {task_id} 已从上一个检查点继续执行（后台，decision 被忽略）。"
        raise ValueError(f"任务 {task_id} 当前状态为 {status}，不能 resume"
                         "（只有 awaiting_human / interrupted / failed 可以）。")

    def result(self, task_id: str) -> dict:
        task = self.tasks.get(task_id)
        if task is None:
            return {"error": f"未知任务：{task_id}"}
        out = {"task_id": task_id, "topic": task.get("topic", ""),
               "status": task["status"]}
        output_path = task.get("output_path", "")
        if task["status"] == "completed" and output_path:
            article = Path(output_path)
            out["output_dir"] = str(article.parent)
            out["article_path"] = output_path
            out["article"] = (article.read_text(encoding="utf-8")
                              if article.exists() else None)
            if task.get("snapshot_commit"):
                out["snapshot_commit"] = task["snapshot_commit"]
        else:
            detail = {
                "awaiting_human": "任务正挂在人工确认点，用 writing_status 查看内容、"
                                  "writing_resume 传入决定。",
                "failed": f"任务失败：{task.get('error', '')}",
                "interrupted": "任务被 server 重启打断，用 writing_resume 从检查点继续。",
            }.get(task["status"], "任务正在执行中，稍后再查。")
            out["detail"] = detail
        return out

    # ---- 内部 ----

    @staticmethod
    def _validate_decision(task: dict, decision: dict) -> None:
        """按挂起的 interrupt 类型检查 resume 值的关键字段，错了当场报错，
        而不是让图跑出莫名其妙的状态。"""
        kind = (task.get("interrupt") or {}).get("kind")
        if kind == "outline" and "approved" not in decision:
            raise ValueError('outline 确认点的 decision 需要 {"approved": bool, "feedback": str}')
        if kind == "final" and decision.get("route") not in _FINAL_ROUTES:
            raise ValueError(
                f'final 确认点的 decision 需要 {{"route": {_FINAL_ROUTES}, "feedback": str}}')

    def _spawn(self, task_id: str, fn, *args) -> None:
        def _run():
            try:
                fn(*args, self._persist, self.checkpoint_db, self.on_saved)
            except Exception as e:  # runner.drive 内部已兜底，这里是最后保险
                task = self.tasks[task_id]
                task["status"] = "failed"
                task["error"] = f"{type(e).__name__}: {e}"
                self._persist(task)

        t = threading.Thread(target=_run, daemon=True,
                             name=f"writing-task-{task_id}")
        self._threads[task_id] = t
        t.start()


manager = TaskManager()
mcp = FastMCP("writing")


@mcp.tool()
def writing_start(topic: str, idea: str = "", auto_approve: bool = True) -> str:
    """启动一个写作任务：后台跑图，立即返回 task_id。

    topic 是文章主题；idea 是可选的思路/方向（对应 CLI 里的"我的想法"）。
    auto_approve=True 时大纲和终审都自动通过、全程无人；False 时在
    大纲确认和终审两个点挂起，等 writing_resume 传决定。
    """
    return manager.start(topic, idea, auto_approve)


@mcp.tool()
def writing_status(task_id: str) -> dict:
    """查任务状态：节点时间线（timeline，每个节点完成时间+耗时）、当前是否挂在
    人工确认点、挂起时的 interrupt payload（kind=outline 时是大纲，kind=final
    时是待确认成稿）、LLM 调用的活性心跳（heartbeat：当前角色、已运行秒数、
    已收思考/正文字数、距上次收到数据的秒数 last_data_ago_s、本条心跳的
    年龄 age_s——age_s 持续很小说明正在正常生成）。"""
    return manager.status(task_id)


@mcp.tool()
def writing_resume(task_id: str, decision: dict) -> str:
    """给挂在人工确认点的任务传入决定，继续执行。

    大纲确认点：{"approved": true, "feedback": ""} 通过；
    {"approved": false, "feedback": "修改意见"} 打回重出大纲。
    终审确认点：{"route": "approve", "feedback": ""} 通过保存；
    route 为 "content" / "style" 分别回 writer 重写 / 回 stylist 重润色。
    状态为 interrupted / failed 时调用则从上一个检查点继续（decision 被忽略），
    已完成的节点不会重跑。
    """
    return manager.resume(task_id, decision)


@mcp.tool()
def writing_result(task_id: str) -> dict:
    """取成稿：output/ 下成稿目录路径 + article.md 全文。
    未完成时返回当前状态说明。"""
    return manager.result(task_id)


if __name__ == "__main__":
    mcp.run()  # 默认 stdio 传输
