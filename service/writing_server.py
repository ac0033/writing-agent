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
from contextvars import copy_context
from copy import deepcopy
from datetime import datetime
from pathlib import Path

from mcp.server.fastmcp import FastMCP

import config
from service import runner, snapshot

TASKS_FILE = config.SERVICE_STATE_DIR / "tasks.json"
HEARTBEAT_FILE = config.SERVICE_STATE_DIR / "heartbeat.json"

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
        self._cancellations = {}
        self._closing = False
        self._writes_closed = False
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
        with self._lock:
            # shutdown交还目录后，迟到API线程只能结束，不能覆盖新管理器登记簿。
            if self._writes_closed:
                return
            task["updated_at"] = _now()
            # 原子写：先落临时文件再替换，避免写一半进程被杀留下坏 json
            self.tasks_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.tasks_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.tasks, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            tmp.replace(self.tasks_path)

    # ---- 工具实现 ----

    def start(self, topic: str, idea: str = "", auto_approve: bool = True, topic_id: str = "", *, topic_file: str = "", topic_sha256: str = "", pipeline_version: str = "v1", sample_requested: bool = False, revision_base: dict | None = None) -> str:
        if pipeline_version not in {"v1", "v2"}:
            raise ValueError("未知流程版本")
        if revision_base is not None and pipeline_version != "v2":
            raise ValueError("修订已有版本只支持 v2 流程")
        from tools.identity import topic_id as resolve_topic_id
        identity = resolve_topic_id(topic, topic_id)
        task_id = uuid.uuid4().hex[:8]
        task = {
            "task_id": task_id,
            "pipeline_version": pipeline_version,
            "sample_requested": bool(sample_requested),
            # 修订任务：以版本线里某一版为起点定向修改（路径、版本号、正文哈希、修改要求），保存后接到线尾。
            "revision_base": dict(revision_base) if revision_base else None,
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
            if self._closing:
                raise ValueError("当前任务管理器正在退出，不能开始新任务")
            self.tasks[task_id] = task
        self._persist(task)
        self._spawn(task_id, runner.start_task, task)
        return task_id

    def status(self, task_id: str) -> dict:
        with self._lock:
            task = deepcopy(self.tasks.get(task_id))
        if task is None:
            return {"error": f"未知任务：{task_id}"}
        awaiting = task["status"] == "awaiting_human"
        return {
            "task_id": task_id,
            "thread_id": task["thread_id"],
            "pipeline_version": task.get("pipeline_version", "v1"),
            "shared_summary": task.get("shared_summary", ""),
            "summary_version": task.get("summary_version", 0),
            "article_version": task.get("article_version", 0),
            "model_calls": task.get("model_calls", 0),
            "model_seconds": task.get("model_seconds", 0),
            "pending_feedback_count": len(task.get("pending_feedback", [])),
            "save_started": task.get("save_started", False),
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
            # 任务级分工覆盖与最近的实际接入记录（每次解析一条，含 AI OS 与专业节点）
            "role_settings": task.get("role_settings", {}),
            "routes": task.get("routes", [])[-12:],
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
        with self._lock:
            status = task["status"]
            if status == "awaiting_human":
                self._validate_decision(task, decision)
                decision = dict(decision)
                self._spawn(task_id, runner.resume_task, task, decision)
                return f"任务 {task_id} 已用传入的 decision 继续执行（后台）。"
            if status in ("interrupted", "failed"):
                # 检查点还在 sqlite 里：从上一个节点边界继续，已完成节点不重跑。
                # failed 常见于 LLM 硬错误（余额/key/网络），修复后从这里续跑。
                self._spawn(task_id, runner.continue_task, task)
                return f"任务 {task_id} 已从上一个检查点继续执行（后台，decision 被忽略）。"
        raise ValueError(f"任务 {task_id} 当前状态为 {status}，不能 resume"
                         "（只有 awaiting_human / interrupted / failed 可以）。")

    def update_input(self, task_id: str, feedback: str) -> dict:
        """在运行边界接收真实用户更新，不并发修改LangGraph检查点。"""
        with self._lock:
            task = self.tasks.get(task_id)
            if not task or task.get("pipeline_version") != "v2":
                raise ValueError("只有v2任务支持共享摘要更新")
            if not isinstance(feedback, str) or not feedback.strip():
                raise ValueError("用户反馈不能为空")
            if task.get("save_started"):
                raise ValueError("当前确认稿已开始保存，未接收本次更新；请保存完成后新建重写任务")
            if task["status"] not in {"pending", "running", "interrupted", "failed"}:
                raise ValueError("待确认时请用writing_resume反馈；已完成稿请新建重写任务")
            item = {"id": uuid.uuid4().hex, "text": feedback.strip(), "created_at": _now()}
            task.setdefault("pending_feedback", []).append(item)
            self._persist(task)
        return {"task_id": task_id, "feedback_id": item["id"], "status": "queued",
                "next_action": "将在AI OS下个安全边界更新共享摘要并检查已生成内容；尚未执行的更新会在恢复后处理"}

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

    def configure_jev(self, task_id: str, operation: dict) -> dict:
        """只接收真实用户设置；运行中不修改授权，下一安全边界同步。"""
        from service.jev_settings import apply_settings, default_settings
        with self._lock:
            task = self.tasks.get(task_id)
            if not task or task.get("pipeline_version") != "v2":
                raise ValueError("仅v2任务支持JEV设置")
            if task["status"] not in {"awaiting_human", "failed", "interrupted"}:
                raise ValueError("请在任务等待确认或暂停时修改JEV设置")
            settings = apply_settings(task.get("jev_settings", default_settings()), operation)
            task["jev_settings"] = settings
            self._persist(task)
            return {"task_id": task_id, "mode": settings["mode"], "settings": settings}

    def configure_roles(self, task_id: str, settings: dict) -> dict:
        """任务级专业节点分工覆盖。任何状态都可设置；runner 在下一节点边界读取，在途调用不受影响。

        settings：{角色: {"provider": ..., "model": ...}}；provider 为空或 None 表示清除该角色的覆盖、回到配置分工。
        """
        from ai_os_connection import ROLE_NAMES
        if not isinstance(settings, dict) or not settings:
            raise ValueError("分工设置必须是 {角色: {provider, model}} 对象")
        with self._lock:
            task = self.tasks.get(task_id)
            if not task:
                raise ValueError(f"未知任务：{task_id}")
            current = dict(task.get("role_settings", {}))
            for role, value in settings.items():
                if role not in ROLE_NAMES:
                    raise ValueError(f"未知角色：{role}")
                if value in (None, "", {}) or (isinstance(value, dict) and not value.get("provider")):
                    current.pop(role, None)
                    continue
                if not isinstance(value, dict) or value.get("provider") not in config.ROLE_PROVIDERS:
                    raise ValueError(f"角色 {role} 的提供方必须是 {', '.join(config.ROLE_PROVIDERS)} 之一")
                model = value.get("model") or ""
                if not isinstance(model, str) or len(model) > 80:
                    raise ValueError("模型名须为不超过 80 字的字符串")
                current[role] = {"provider": value["provider"], "model": model}
            task["role_settings"] = current
            task.setdefault("role_events", []).append({"source": "user", "settings": current, "at": _now()})
            self._persist(task)
            return {"task_id": task_id, "role_settings": current,
                    "applies": "下一节点边界起生效；正在执行的调用沿用其开始时的分工"}

    def extend_model_budget(self, task_id: str, calls: int = 0, seconds: int = 0):
        with self._lock:
            task = self.tasks.get(task_id)
            if not task or task["status"] not in {"failed", "interrupted", "awaiting_human"}:
                raise ValueError("只能在暂停任务上明确追加预算")
            if type(calls) is not int or type(seconds) is not int or not 0 <= calls <= 120 or not 0 <= seconds <= 86400 or not (calls or seconds):
                raise ValueError("追加调用数为0至120、秒数为0至86400，至少一项非零")
            task["additional_model_calls"] = task.get("additional_model_calls", 0) + calls
            task["additional_seconds"] = task.get("additional_seconds", 0) + seconds
            task.setdefault("budget_events", []).append({"source": "user", "calls": calls, "seconds": seconds, "at": _now()})
            self._persist(task)
            return {"task_id": task_id, "additional_model_calls": task["additional_model_calls"], "additional_seconds": task["additional_seconds"]}

    # ---- 内部 ----

    @staticmethod
    def _validate_decision(task: dict, decision: dict) -> None:
        """按挂起的 interrupt 类型检查 resume 值的关键字段，错了当场报错，
        而不是让图跑出莫名其妙的状态。"""
        kind = (task.get("interrupt") or {}).get("kind")
        if not isinstance(decision, dict):
            raise ValueError("decision必须是对象")
        if task.get("pipeline_version") == "v2":
            payload = task.get("interrupt") or {}
            if kind == "summary" and decision.get("route") == "approve":
                raise ValueError("摘要确认只接受 approved=true 与所审阅版本，不能用 route 代替")
            approving = ((kind == "summary" and decision.get("approved") is True)
                         or (kind == "final" and decision.get("route") == "approve"))
            if approving and str(decision.get("feedback", "")).strip():
                raise ValueError("确认与修改意见不能同时提交，请先提交修改意见并查看更新结果")
            required = ("summary_version",) if (kind == "summary" and decision.get("approved") is True) or (kind == "sample" and decision.get("choice")) else (
                ("summary_version", "article_version") if kind == "final" and decision.get("route") == "approve" else ())
            for field in required:
                if type(decision.get("expected_" + field)) is not int:
                    raise ValueError("批准必须提供所审阅版本：expected_" + field)
            for field in ("summary_version", "article_version"):
                expected = decision.get("expected_" + field)
                current = payload.get(field, task.get(field, 0))
                if expected is not None and (type(expected) is not int or expected != current):
                    raise ValueError("用户确认对应的版本已经变化，请重新查看：" + field)
            if kind == "summary" and not isinstance(decision.get("approved"), bool):
                raise ValueError("摘要确认需要 approved 布尔值及可选feedback")
            if kind == "final" and decision.get("route") not in {"approve", "feedback"}:
                raise ValueError("成稿确认需要 route=approve 或 feedback")
            if kind == "sample":
                choice = decision.get("choice")
                if choice is not None and choice not in payload.get("options", {}):
                    raise ValueError("短样稿选择不在当前展示选项中")
                if not choice and not str(decision.get("feedback", "")).strip():
                    raise ValueError("请选择短样稿或提供修改意见")
            if kind not in {"summary", "final", "sample"} and not isinstance(decision.get("feedback"), str):
                raise ValueError("请提供用户的自然语言feedback")
            return
        if kind == "outline" and not isinstance(decision.get("approved"), bool):
            raise ValueError('outline 确认点的 decision 需要 {"approved": bool, "feedback": str}')
        if kind == "final" and decision.get("route") not in _FINAL_ROUTES:
            raise ValueError(
                f'final 确认点的 decision 需要 {{"route": {_FINAL_ROUTES}, "feedback": str}}')

    def _spawn(self, task_id: str, fn, *args) -> None:
        from service.cancellation import CancellationToken, TaskCancelled, current_cancellation
        def _run():
            from log import heartbeat_task
            token = heartbeat_task.set(task_id)
            state_lock_token = runner.task_state_lock.set(self._lock)
            cancellation_context = current_cancellation.set(cancellation)
            try:
                cancellation.check()
                from tools.storage import exclusive
                lock_path = self.tasks_path.parent / ("." + task_id + ".lock")
                with exclusive(lock_path):
                    fn(*args, self._persist, self.checkpoint_db, self.on_saved)
            except TaskCancelled:
                with self._lock:
                    task = self.tasks[task_id]
                    task["status"] = "interrupted"
                    task["error"] = "任务已取消；断点与已用模型预算保留"
                    self._persist(task)
            except Exception as e:  # runner.drive 内部已兜底，这里是最后保险
                task = self.tasks[task_id]
                task["status"] = "failed"
                task["error"] = runner.safe_error(e)
                self._persist(task)
            finally:
                current_cancellation.reset(cancellation_context)
                runner.task_state_lock.reset(state_lock_token)
                heartbeat_task.reset(token)

        with self._lock:
            if self._closing:
                raise ValueError("当前任务管理器正在退出，不能继续执行")
            previous = self._threads.get(task_id)
            if previous and previous.is_alive():
                raise ValueError("该任务仍在运行，不能重复恢复")
            self.tasks[task_id]["status"] = "pending"
            self.tasks[task_id]["error"] = ""
            self._persist(self.tasks[task_id])
            # 接入配置和密钥只随本次执行上下文传递，不进入任务JSON或checkpoint。
            context = copy_context()
            cancellation = CancellationToken()
            self._cancellations[task_id] = cancellation
            t = threading.Thread(target=lambda: context.run(_run), daemon=True, name=f"writing-task-{task_id}")
            self._threads[task_id] = t
            t.start()

    def shutdown(self, timeout=8.0):
        """只取消本管理器拥有的执行；总等待有上限，不等待API数分钟。"""
        deadline = time.monotonic() + max(0.0, timeout)
        with self._lock:
            self._closing = True
            owned = [(identity, thread, self._cancellations.get(identity))
                     for identity, thread in self._threads.items() if thread.is_alive()]
            for identity, _, token in owned:
                if token and self.tasks[identity].get("status") != "completed":
                    token.request_cancel()
        for _, _, token in owned:
            if token and token.event.is_set():
                token.settle_inflight()
        for _, thread, _ in owned:
            if thread is not threading.current_thread():
                thread.join(timeout=max(0.0, deadline - time.monotonic()))
        for _, _, token in owned:
            if token and token.event.is_set():
                token.settle_inflight()
        with self._lock:
            for identity, _, token in owned:
                if token and token.event.is_set() and self.tasks[identity].get("status") in {"pending", "running"}:
                    task = self.tasks[identity]
                    task["status"] = "interrupted"
                    task["error"] = "退出已取消本地任务；已发送的API请求不保证远端停止计费"
                    self._persist(task)
            result = {"remaining_tasks": [identity for identity, thread, _ in owned if thread.is_alive()],
                      "remaining_cli": [identity for identity, _, token in owned if token and token.live_process_count()]}
            if not result["remaining_cli"]:
                self._writes_closed = True
            return result


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
                  topic_id: str = "", topic_file: str = "", pipeline_version: str = "v1") -> str:
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
    if prepared.get("schema") == "writing-source-v2":
        pipeline_version = "v2"
    if (topic and topic != prepared["topic"]) or (topic_id and topic_id != prepared["topic_id"]):
        raise ValueError("标题或主题标识与主题文件不一致，请使用文件中的值或重新保存提炼")
    return manager.start(prepared["topic"], prepared["idea"], auto_approve, prepared["topic_id"],
                         topic_file=prepared["topic_file"], topic_sha256=prepared["topic_sha256"],
                         pipeline_version=pipeline_version)


@mcp.tool()
def writing_start_v2(topic: str, source_text: str, topic_id: str = "", sample_requested: bool = False) -> str:
    """新流程：提交用户原话及本篇相关材料，AI OS整理摘要并等待用户确认。

    不要求外层先替用户写大纲。不自动确认摘要、成稿或发布。
    source_text仅含用户授权本篇使用的材料；原输入另存，旧主题不覆盖。
    """
    from tools.topics import prepare_source
    prepared = prepare_source(topic, source_text, topic_id)
    return manager.start(prepared["topic"], prepared["idea"], False, prepared["topic_id"],
                         topic_file=prepared["topic_file"], topic_sha256=prepared["topic_sha256"],
                         pipeline_version="v2", sample_requested=sample_requested)


@mcp.tool()
def writing_configure_jev(task_id: str, operation: dict) -> dict:
    """维护用户明确偏好或JEV权限，不能从模型建议伪造用户授权。见docs/jev验收计划.md。"""
    return manager.configure_jev(task_id, operation)


@mcp.tool()
def writing_configure_roles(task_id: str, settings: dict) -> dict:
    """为某任务显式改专业节点分工（设置来自用户在页面或本工具的操作）：{角色: {"provider": "claude|codex|codebuddy|deepseek|dashscope", "model": ""}}。
    provider 留空表示恢复配置分工。下一节点边界生效，在途调用不受影响；实际接入见 writing_status 的 routes。"""
    return manager.configure_roles(task_id, settings)


@mcp.tool()
def writing_extend_budget(task_id: str, calls: int = 0, seconds: int = 0) -> dict:
    """仅在用户明确同意增加全篇调用/时间预算后调用，历史用量不重置。"""
    return manager.extend_model_budget(task_id, calls, seconds)


@mcp.tool()
def writing_status(task_id: str) -> dict:
    """查任务状态：节点时间线（timeline，每个节点完成时间+耗时）、当前是否挂在
    人工确认点、挂起时的 interrupt payload（kind=outline 时是大纲，kind=final
    时是待确认成稿）、LLM 调用的活性心跳（heartbeat：当前角色、已运行秒数、
    已收思考/正文字数、距上次收到数据的秒数 last_data_ago_s、本条心跳的
    年龄 age_s——age_s 持续很小说明正在正常生成）。"""
    return manager.status(task_id)


@mcp.tool()
def writing_update_input(task_id: str, feedback: str) -> dict:
    """把用户新的观点或纠正排入v2运行，在AI OS安全边界更新共享摘要；不代用户确认成稿。"""
    return manager.update_input(task_id, feedback)


@mcp.tool()
def writing_resume(task_id: str, decision: dict) -> str:
    """给挂在人工确认点的任务传入用户明确决定，继续执行。

    final 的 approve 只能在用户明确确认文章无误并同意保存后传入；它不授权发布。

    v2摘要确认：{"approved": true, "expected_summary_version": 展示的摘要版本}。
    v2终审确认：{"route": "approve", "expected_summary_version": 展示的摘要版本,
    "expected_article_version": 展示的稿件版本}。批准版本不可省略或根据后来的状态补填。
    v2修改使用route="feedback"和自然语言feedback；以下content/style仅适用于v1。

    大纲确认点：{"approved": true, "feedback": ""} 通过；
    {"approved": false, "feedback": "修改意见"} 打回重出大纲。
    终审确认点：{"route": "approve", "feedback": ""} 通过保存；
    route 为 "content" / "style" 分别回 writer 重写 / 回 stylist 重润色。
    状态为 interrupted / failed 时调用则从上一个检查点继续（decision 被忽略），
    已完成的节点不会重跑。
    """
    # v2批准必须包含展示时的expected_summary_version；终审另含expected_article_version。
    # 不替调用者从当前状态补批准版本，避免旧确认意外批准新稿。
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
