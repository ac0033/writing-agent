"""FastMCP server（stdio）：把写作工作流暴露成 MCP 工具。

工具：
- writing_topic_guide() / writing_prepare_topic(...)：聊天提炼规范与主题 Markdown 保存。
- writing_start(topic_file=已保存的主题路径, auto_approve=True) -> task_id
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

成稿经用户确认并保存后仅快照本篇产物；发布需另行展示预览并取得确认。

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
    return snapshot.git_snapshot(config.BASE_DIR, f"post: {task.get('topic', '?')}",
                                 paths=[Path(task["output_path"]).parent])


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
        self._lock = threading.RLock()
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

    def start(self, topic: str, idea: str = "", auto_approve: bool = True, topic_id: str = "", *, topic_file: str = "", topic_sha256: str = "") -> str:
        from tools.identity import topic_id as resolve_topic_id
        identity = resolve_topic_id(topic, topic_id)
        task_id = uuid.uuid4().hex[:8]
        task = {
            "task_id": task_id,
            "thread_id": f"svc-{task_id}",  # 与 CLI 会话的 thread_id 区分，互不串场
            "topic": topic,
            "topic_id": identity,
            "idea": idea,
            "topic_file": topic_file,
            "topic_sha256": topic_sha256,
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
            "topic_id": task.get("topic_id", ""),
            "topic_file": task.get("topic_file", ""),
            "topic_sha256": task.get("topic_sha256", ""),
            "status": task["status"],
            "progress": task.get("progress", []),
            "next_nodes": task.get("next_nodes", []),
            # 节点级时间线：[{node, at, dur_s}]，实时反映跑到哪、每步多久
            "timeline": task.get("timeline", []),
            # 节点内 LLM 调用的活性心跳：判断"在生成"还是"卡死"的依据
            "heartbeat": self._read_heartbeat(task_id) if task["status"] == "running" else None,
            "publication_ready": task.get("publication_ready", False),
            "quality_issues": task.get("quality_issues", []),
            "memory_result": task.get("memory_result", {}),
            "awaiting_human": awaiting,
            # 挂起时把 payload（大纲/成稿）带出来，供外部审阅后决定 resume 值
            "interrupt": task.get("interrupt") if awaiting else None,
            "output_path": task.get("output_path", ""),
            "error": task.get("error", ""),
            "created_at": task.get("created_at", ""),
            "updated_at": task.get("updated_at", ""),
        }

    @staticmethod
    def _read_heartbeat(task_id: str) -> dict | None:
        """读心跳文件并补一个 age_s（距现在几秒）。读不到就返回 None。"""
        try:
            path = HEARTBEAT_FILE.with_name(HEARTBEAT_FILE.stem + "-" + task_id + HEARTBEAT_FILE.suffix)
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("task_id") != task_id:
                return None
            data["age_s"] = round(time.time() - data.get("ts", 0), 1)
            return data
        except (OSError, json.JSONDecodeError):
            return None

    def resume(self, task_id: str, decision: dict) -> str:
        task = self.tasks.get(task_id)
        if task is None:
            raise ValueError(f"未知任务：{task_id}")
        # runner 已报告等待确认时，线程可能还在关闭 checkpoint 连接。
        # 不持有登记簿锁等待收尾，避免用户看到可确认却被误判为重复恢复。
        previous = self._threads.get(task_id)
        if task["status"] in ("awaiting_human", "failed", "interrupted") and previous and previous.is_alive():
            previous.join(timeout=5)
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
            out["next_action"] = "稿件已保存本地。请先展示 writing_publish_preview，再单独询问用户是否同意发布；未获明确同意不得调用 writing_publish。"
            out["publication"] = task.get("publication", {"status": "not_published"})
            out["quality_issues"] = task.get("quality_issues", [])
            out["memory_result"] = task.get("memory_result", {})
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
        if kind == "outline" and not isinstance(decision.get("approved"), bool):
            raise ValueError('outline 确认点的 decision 需要 {"approved": bool, "feedback": str}')
        if kind == "final" and decision.get("route") not in _FINAL_ROUTES:
            raise ValueError(
                f'final 确认点的 decision 需要 {{"route": {_FINAL_ROUTES}, "feedback": str}}')

    def _spawn(self, task_id: str, fn, *args) -> None:
        def _run():
            from log import heartbeat_task
            token = heartbeat_task.set(task_id)
            try:
                from tools.storage import exclusive
                lock_path = self.tasks_path.parent / ("." + task_id + ".lock")
                with exclusive(lock_path):
                    fn(*args, self._persist, self.checkpoint_db, self.on_saved)
            except Exception as e:  # runner.drive 内部已兜底，这里是最后保险
                task = self.tasks[task_id]
                task["status"] = "failed"
                task["error"] = f"{type(e).__name__}: {e}"
                self._persist(task)
            finally:
                heartbeat_task.reset(token)

        with self._lock:
            previous = self._threads.get(task_id)
            if previous and previous.is_alive():
                raise ValueError("该任务仍在运行，不能重复恢复")
            self.tasks[task_id]["status"] = "pending"
            self.tasks[task_id]["error"] = ""
            self._persist(self.tasks[task_id])
            t = threading.Thread(target=_run, daemon=True, name=f"writing-task-{task_id}")
            self._threads[task_id] = t
            t.start()


manager = TaskManager()
mcp = FastMCP("writing")


@mcp.tool()
def writing_topic_guide() -> str:
    """启动写作前先读取：如何把宿主聊天提炼成带具体论据的金字塔主题，含 Markdown 模板。"""
    return (config.PROMPTS_DIR / "chat_to_topic.md").read_text(encoding="utf-8")


@mcp.tool()
def writing_prepare_topic(topic: str, pyramid_markdown: str, topic_id: str = "") -> dict:
    """保存聊天金字塔提炼到 writing/topic，返回实际文件路径和内容，不启动写作。

    先调用 writing_topic_guide 读取模板；外层 agent 必须先读真实聊天再提炼。
    二级章节依次为核心命题、金字塔总览、分层论点与具体论据、关键模型、
    边界与待验证问题、对话来源。每个 - **论点**： 下面填写具体素材、支持关系、
    材料性质、对话定位四个子项。缺材料明确留缺口，不编造，不把假设当实证。
    同主题沿用 topic_id；新提炼另存文件，保留旧文件和人工修改。
    """
    from tools.topics import prepare
    return prepare(topic, pyramid_markdown, topic_id)


@mcp.tool()
def writing_start(topic: str = "", idea: str = "", auto_approve: bool = True,
                  topic_id: str = "", topic_file: str = "") -> str:
    """仅从已保存的金字塔主题启动后台写作，返回 task_id。

    必须先 writing_topic_guide → writing_prepare_topic，
    展示保存路径，再传 topic_file。写作输入从该 Markdown 实际读取，不能直接传聊天原文。
    idea 已停用；topic/topic_id 可省略，传入则必须与文件一致。
    auto_approve=True 仅自动通过大纲，False 时大纲也需确认；成稿与发布始终分别确认。
    """
    from tools.topics import load
    if idea:
        raise ValueError("不能直接传 idea；先将聊天金字塔提炼保存为主题文件，再传 topic_file")
    prepared = load(topic_file)
    if (topic and topic != prepared["topic"]) or (topic_id and topic_id != prepared["topic_id"]):
        raise ValueError("标题或主题标识与主题文件不一致，请使用文件中的值或重新保存提炼")
    return manager.start(prepared["topic"], prepared["idea"], auto_approve, prepared["topic_id"],
                         topic_file=prepared["topic_file"], topic_sha256=prepared["topic_sha256"])


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
    """给挂在人工确认点的任务传入用户明确决定，继续执行。

    final 的 approve 只能在用户明确确认文章无误并同意保存后传入；它不授权发布。

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


@mcp.tool()
def writing_publish_preview(task_id: str) -> dict:
    """文章经用户确认并保存后，展示发布目标与内容指纹。仅预览，不发布。"""
    from tools.publishing import preview
    task = manager.tasks.get(task_id)
    if not task or task.get("status") != "completed":
        raise ValueError("请先由用户确认文章无误并完成本地保存")
    return preview(task["output_path"])


@mcp.tool()
def writing_publish(task_id: str, approval_token: str, confirmed: bool = False) -> dict:
    """仅在保存后向用户展示发布预览、并单独取得明确发布同意时调用。

    大纲确认、文章确认、auto_approve、此前泛化授权均不代表本次发布同意。
    approval_token 必须来自用户刚审阅的 writing_publish_preview。
    """
    from tools.publishing import publish
    task = manager.tasks.get(task_id)
    if not task or task.get("status") != "completed":
        raise ValueError("稿件尚未完成本地保存")
    result = publish(task["output_path"], confirmed=confirmed, approval_token=approval_token)
    task["publication"] = result
    manager._persist(task)
    return result


if __name__ == "__main__":
    mcp.run()  # 默认 stdio 传输
