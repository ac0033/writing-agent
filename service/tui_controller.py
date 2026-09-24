"""终端界面复用生产任务管理器；按钮确认绑定界面实际展示的版本。"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from pathlib import Path
from threading import RLock

from service.writing_server import TaskManager


class TuiController:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.manager = TaskManager(directory / "tasks.json", directory / "checkpoints.sqlite", on_saved=None)
        self.task_id = ""
        self.displayed = None
        self.publish_preview = None
        self._view_lock = RLock()

    def action_snapshot(self):
        with self._view_lock:
            return self.task_id, deepcopy(self.displayed)

    def execute(self, action, args, task_id, displayed):
        """线程动作绑定点击时的任务与所见内容，切换历史任务后不能误操作新任务。"""
        if action not in {"start", "send", "approve", "retry", "preference", "preview", "publish", "extend_budget", "configure_roles"}:
            raise ValueError("未知终端动作")
        with self._view_lock:
            if self.task_id != task_id:
                raise ValueError("当前任务已经切换，请在目标任务重新操作")
            if action in {"approve", "send"} and self.displayed != displayed:
                raise ValueError("待确认内容已经变化，请重新查看后操作")
            if action in {"preview", "publish"}:
                plan = deepcopy(self.publish_preview)
            else:
                return getattr(self, action)(*args)
        # 发布可能等待Git网络，不能锁住UI的状态与任务选择。
        if action == "preview":
            return self.preview(task_id)
        return self.publish(task_id, plan)

    def clear(self):
        with self._view_lock:
            self.task_id, self.displayed = "", None
            self.publish_preview = None

    def invalidate_publish_preview(self):
        with self._view_lock:
            self.publish_preview = None

    def show_publish_preview(self, result):
        with self._view_lock:
            if result.get("task_id") != self.task_id:
                raise ValueError("当前任务已切换，请重新预览发布内容")
            self.publish_preview = deepcopy(result)

    def preview(self, task_id=None):
        from tools.publishing import preview
        import hashlib
        identity = task_id or self.task_id
        status = self.manager.status(identity)
        if status.get("status") != "completed" or not status.get("output_path"):
            raise ValueError("只能预览已由用户终审并保存的稿件")
        plan = preview(status["output_path"])
        data = Path(plan["article_path"]).read_bytes()
        if hashlib.sha256(data).hexdigest() != plan["article_sha256"]:
            raise ValueError("预览期间正文已变化，请重新预览")
        return {"task_id": identity, "plan": plan, "article": data.decode("utf-8")}

    def publish(self, task_id=None, displayed_plan=None):
        from tools.publishing import publish
        identity = task_id or self.task_id
        preview = displayed_plan if displayed_plan is not None else deepcopy(self.publish_preview)
        if not preview or preview.get("task_id") != identity:
            raise ValueError("请先查看当前任务的完整发布预览，再单独确认发布")
        status = self.manager.status(identity)
        plan = preview["plan"]
        if status.get("status") != "completed" or Path(status.get("output_path", "")).resolve() != Path(plan["article_path"]).resolve():
            raise ValueError("任务的已保存稿件已变化，请重新预览")
        result = publish(plan["article_path"], confirmed=True, approval_token=plan["approval_token"])
        with self.manager._lock:
            task = self.manager.tasks[identity]
            task["publication"] = result
            self.manager._persist(task)
        with self._view_lock:
            if self.task_id == identity:
                self.publish_preview = None
        return {"task_id": identity, **result}

    def start(self, topic: str, source: str, sample_requested=False):
        if not topic.strip() or not source.strip():
            raise ValueError("请填写主题与观点材料")
        if self.task_id and self.status().get("status") in {"pending", "running"}:
            raise ValueError("当前任务仍在运行")
        from tools.topics import prepare_source
        prepared = prepare_source(topic, source)
        self.task_id = self.manager.start(topic, prepared["idea"], False, prepared["topic_id"],
            topic_file=prepared["topic_file"], topic_sha256=prepared["topic_sha256"], pipeline_version="v2",
            sample_requested=sample_requested)
        self.displayed = None
        return self.task_id

    def select(self, task_id):
        with self._view_lock:
            if task_id not in self.manager.tasks:
                raise ValueError("任务不存在")
            self.task_id, self.displayed = task_id, None
            self.publish_preview = None

    def status(self):
        with self._view_lock:
            return deepcopy(self.manager.status(self.task_id)) if self.task_id else {}

    def history_options(self):
        with self.manager._lock:
            return [(task.get("topic", key)[:18] + " · " + key, key)
                    for key, task in self.manager.tasks.items()]

    def show_interrupt(self, payload):
        with self._view_lock:
            self.displayed = deepcopy(payload)

    def approve(self):
        payload = self.displayed
        if not payload:
            raise ValueError("请先查看待确认内容")
        kind = payload.get("kind")
        decision = {"expected_summary_version": payload.get("summary_version")}
        if kind == "summary":
            decision["approved"] = True
        elif kind == "final":
            decision.update(route="approve", expected_article_version=payload.get("article_version"))
        else:
            raise ValueError("请用自然语言回答当前问题，不能一键批准")
        result = self.manager.resume(self.task_id, decision)
        self.displayed = None
        return result

    def send(self, text):
        if not text.strip():
            raise ValueError("消息不能为空")
        status = self.status()
        if status.get("awaiting_human"):
            payload = self.displayed
            if not payload:
                raise ValueError("请先查看当前待确认内容")
            decision = {"feedback": text, "approved": False, "route": "feedback",
                        "expected_summary_version": payload.get("summary_version"),
                        "expected_article_version": payload.get("article_version")}
            if payload.get("kind") == "final" and status.get("pipeline_version") != "v2":
                # 旧流程（v1）终审只认 approve / content / style；“退回修改”对应 content（回初稿重写并重新审核）。
                decision = {"route": "content", "feedback": text}
            if payload.get("kind") == "sample" and text.strip().upper() in {"A", "B"}:
                decision["choice"] = text.strip().upper()
                decision["feedback"] = ""
            result = self.manager.resume(self.task_id, decision)
            self.displayed = None
            return result
        return self.manager.update_input(self.task_id, text)

    def retry(self):
        if self.status().get("status") not in {"failed", "interrupted"}:
            raise ValueError("只可续跑失败或被中断的任务；待确认内容请明确回应")
        return self.manager.resume(self.task_id, {})

    def budget_snapshot(self):
        """表单打开时绑定任务并展示现有用量；读快照不授权任何追加。"""
        import config
        with self._view_lock, self.manager._lock:
            task = self.manager.tasks.get(self.task_id)
            if not task or task.get("pipeline_version") != "v2":
                raise ValueError("请先选择 v2 任务")
            if task.get("status") not in {"failed", "interrupted", "awaiting_human"}:
                raise ValueError("只能在任务失败、暂停或等待人工时追加预算")
            payload = task.get("interrupt") or {}
            paused = (task["status"] == "awaiting_human" and payload.get("kind") == "decision"
                      and bool(payload.get("pause_reason")))
            return {"task_id": self.task_id, "topic": task.get("topic", ""), "status": task["status"],
                "model_calls": task.get("model_calls", 0), "model_seconds": task.get("model_seconds", 0),
                "call_limit": config.AI_OS_MAX_MODEL_CALLS + task.get("additional_model_calls", 0),
                "seconds_limit": config.AI_OS_MAX_SECONDS + task.get("additional_seconds", 0),
                "paused": paused, "pause_reason": payload.get("pause_reason", "") if paused else "",
                "ai_os_steps": payload.get("ai_os_steps") if paused else None}

    def extend_budget(self, calls, seconds, steps=0, revisions=0):
        """请求次数/秒数是任务级预算，只登记不续跑；调度步数/修订次数是图内预算，
        只有 AI OS 因预算暂停时才能交回暂停点，交回即从暂停点继续（这不是对任何稿件的确认）。"""
        for value, limit in ((steps, 100), (revisions, 10)):
            if type(value) is not int or not 0 <= value <= limit:
                raise ValueError("追加调度步数为0至100、修订次数为0至10")
        # execute 已在同一 view lock 中核对打开表单时的任务，manager再核对最新运行状态。
        with self._view_lock, self.manager._lock:
            snapshot = self.budget_snapshot()
            if (steps or revisions) and not snapshot["paused"]:
                raise ValueError("只有 AI OS 因调度或修订预算暂停时才能追加调度步数或修订次数")
            if not (calls or seconds or steps or revisions):
                raise ValueError("追加调用数为0至120、秒数为0至86400，至少一项非零")
            result = {"task_id": self.task_id}
            if calls or seconds:
                result.update(self.manager.extend_model_budget(self.task_id, calls, seconds))
            if steps or revisions:
                task = self.manager.tasks[self.task_id]
                task.setdefault("budget_events", []).append({"source": "user", "steps": steps, "revisions": revisions,
                                                             "at": datetime.now().isoformat(timespec="seconds")})
                self.manager._persist(task)
        resumed = False
        if steps or revisions:
            self.manager.resume(self.task_id, {"feedback": "", "additional_steps": steps, "additional_revisions": revisions})
            resumed = True
            with self._view_lock:
                self.displayed = None
        return {**result, "added_calls": calls, "added_seconds": seconds,
                "added_steps": steps, "added_revisions": revisions, "resumed": resumed}

    def role_snapshot(self):
        """打开分工表单时的现状：配置分工、任务级覆盖、最近实际接入。只读，不授权任何切换。"""
        import config
        with self._view_lock, self.manager._lock:
            task = self.manager.tasks.get(self.task_id)
            if not task:
                raise ValueError("请先开始或选择一个任务，再设置分工")
            return {"task_id": self.task_id, "topic": task.get("topic", ""), "config": dict(config.ROLE_MODELS),
                    "overrides": dict(task.get("role_settings", {})), "routes": list(task.get("routes", []))[-7:],
                    "status": task.get("status", "")}

    def configure_roles(self, settings):
        return self.manager.configure_roles(self.task_id, settings)

    def preference(self, text):
        import uuid
        return self.manager.configure_jev(self.task_id, {"action": "preference", "text": text,
            "id": uuid.uuid4().hex, "source_ref": "TUI用户偏好输入", "scope": self.status()["topic_id"],
            "user_event_id": uuid.uuid4().hex})


HISTORY_SOURCES_FILE = "history_sources.json"


def load_history_sources(home: Path) -> dict:
    """本机登记的额外历史来源（在运行目录里，不入库）：

    task_dirs：其他任务目录（含 tasks.json 与 checkpoints.sqlite），任务留在原处，选中时页面切换到该目录；
    articles：没有任务记录的已发布文章（如管道外改定的稿），只读查看，[{"path": 正文, "note": 说明文件}]。
    相对路径以仓库根为基准。
    """
    import json
    import config
    try:
        value = json.loads((Path(home) / HISTORY_SOURCES_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"task_dirs": [], "articles": []}
    resolve = lambda item: (config.BASE_DIR / item).resolve() if not Path(item).is_absolute() else Path(item)
    dirs = [resolve(item) for item in value.get("task_dirs", []) if isinstance(item, str)]
    articles = [{"path": resolve(item["path"]), "note": resolve(item["note"]) if item.get("note") else None}
                for item in value.get("articles", []) if isinstance(item, dict) and item.get("path")]
    return {"task_dirs": [d for d in dirs if (d / "tasks.json").is_file()],
            "articles": [a for a in articles if a["path"].is_file()]}


def read_task_list(directory: Path) -> list[tuple[str, str, str]]:
    """只读列出某任务目录的任务 [(主题, 状态, 任务ID)]，按更新时间倒序；不实例化任务管理器，不触碰检查点。"""
    import json
    try:
        tasks = json.loads((Path(directory) / "tasks.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    rows = sorted(tasks.items(), key=lambda kv: kv[1].get("updated_at", ""), reverse=True)
    return [(task.get("topic", key), task.get("status", ""), key) for key, task in rows if isinstance(task, dict)]


def article_title(path: Path) -> str:
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return Path(path).parent.name
