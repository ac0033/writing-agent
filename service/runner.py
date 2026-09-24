"""写作图的自动驱动循环（service 层核心）。

职责：在给定 thread_id 上跑 LangGraph 图，处理 human_outline / human_final
两个人工确认节点的 interrupt：

- auto_approve=True：outline 自动 resume {"approved": True, "feedback": ""}；
  final 始终挂起，用户明确确认无误后才能保存本地。
- auto_approve=False：遇到 interrupt 就把 payload（大纲/待确认成稿）记进
  task["interrupt"] 并挂起（status="awaiting_human"），等外部调 resume_task
  传入 resume 值继续。

恢复执行用 LangGraph 的 Command(resume=...)（与 main.py 人工循环同一机制）。
存档走 SqliteSaver（checkpoint_db），所以挂起的任务跨进程、跨 server 重启
都能续跑。

任务状态是可 JSON 序列化的普通 dict，由调用方拥有；runner 每次状态变化
调 persist(task) 回调，由调用方决定存哪（server 存 service/tasks.json，
测试存内存）。runner 自己不做任何文件持久化。

memory 开关：config.MEMORY_ENABLED 在 import 时就读 MEMORY_ENABLED 环境变量
（MOCK_LLM 模式下强制关闭），核心代码零改动即可支持。这里在每次驱动前再
核对一次环境变量做兜底——防止 config 先于环境变量设置就被 import 的时序问题。
"""
import os
import time
from contextlib import nullcontext
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path

from langgraph.types import Command

import config
from graph import build_graph
from service.cancellation import TaskCancelled, check_cancelled, current_cancellation


def safe_error(error, extra_secrets=()):
    """接入密钥不应随异常进入任务JSON或终端日志。"""
    from ai_os_connection import current_connection
    connection = current_connection()
    secrets = list(extra_secrets) + [connection.api_key if connection else ""]
    secrets += [value.get("api_key", "") for value in config.PROVIDERS.values() if isinstance(value, dict)]
    secrets += [value for key, value in os.environ.items() if key.endswith(("API_KEY", "ACCESS_TOKEN"))]
    message = f"{type(error).__name__}: {error}"
    for secret in sorted({s for s in secrets if isinstance(s, str) and s}, key=len, reverse=True):
        message = message.replace(secret, "[REDACTED]")
    return message


# TaskManager只在短状态事务中使用此锁；模型执行和保存IO不持锁。
task_state_lock = ContextVar("writing_task_state_lock", default=None)


def _state_guard():
    return task_state_lock.get() or nullcontext()


def _feedback_snapshot(task):
    with _state_guard():
        return list(task.get("pending_feedback", []))


def _commit_save_gate(task, state, persist):
    """最后排空反馈与关闭输入原子完成，之后才能开始保存。"""
    from pipeline_v2 import _drain_feedback
    while True:
        check_cancelled()
        # 合并反馈可能调用AI OS，不能在登记簿锁内等模型，阻塞新反馈。
        pending = _drain_feedback(state)
        if pending:
            return pending
        with _state_guard():
            check_cancelled()
            processed = {row.get("user_feedback_id") for row in state.get("decision_log", [])}
            if any(row["id"] not in processed for row in task.get("pending_feedback", [])):
                continue
            task["save_started"] = True
            persist(task)
            return {}


def graph_for(task: dict, checkpointer=None):
    """流程版本跟随任务保存，旧断点不因默认配置变化套用新图。"""
    if task.get("pipeline_version", "v1") == "v2":
        from pipeline_v2 import build_graph_v2
        return build_graph_v2(checkpointer=checkpointer)
    return build_graph(checkpointer=checkpointer)

# resume 值的格式与 main.py handle_interrupt 保持一致（graph.py:266/445 的契约）
AUTO_RESUME = {
    "outline": {"approved": True, "feedback": ""},
}


def initial_state(topic: str, idea: str, thread_id: str, topic_id: str = "") -> dict:
    """图的初始 state，字段照抄 main.py 的启动逻辑（main.py:235-241）。"""
    from tools.identity import topic_id as resolve_topic_id
    return {"topic": topic, "topic_id": resolve_topic_id(topic, topic_id), "user_idea": idea, "thread_id": thread_id,
            "outline_feedback": [], "materials": [], "review_cycles": 0,
            "research_rounds": 0, "thinking_log": []}


def _record_progress(task: dict, graph, cfg: dict) -> None:
    """从检查点状态提取进度：已完成的 LLM 节点（thinking_log）+ 下一步节点。"""
    try:
        st = graph.get_state(cfg)
        handled = {row.get("user_feedback_id") for row in st.values.get("decision_log", []) if row.get("user_feedback_id")}
        with _state_guard():
            task["progress"] = [e.get("node", "?")
                                for e in st.values.get("thinking_log", [])]
            task["next_nodes"] = list(st.next)
            task["shared_summary"] = st.values.get("shared_summary", "")
            task["summary_version"] = st.values.get("summary_version", 0)
            task["article_version"] = st.values.get("article_version", 0)
            task["additional_graph_seconds"] = st.values.get("additional_seconds", 0)
            task["pending_feedback"] = [row for row in task.get("pending_feedback", []) if row["id"] not in handled]
    except Exception:
        pass  # 进度只是观测信息，拿不到不影响主流程


def _stream_graph(task: dict, graph, first_input, cfg: dict, persist, boundary=None) -> dict | None:
    """跑图直到结束或 interrupt，逐节点事件记 timeline 并实时持久化。

    用 stream（而不是 invoke）是为了拿到节点级事件：每个节点完成就往
    task["timeline"] 追加一条 {node, at, dur_s} 并 persist，外部轮询
    writing_status 能实时看到跑到哪了。dur_s 是距上一个节点事件的秒数，
    依次拼起来就是各节点耗时。

    返回 interrupt payload（挂在人工确认点）或 None（跑完）。
    """
    timeline = task.setdefault("timeline", [])
    t0 = time.monotonic()
    interrupt_payload = None
    check_cancelled()
    if boundary:
        boundary()  # 节点边界：读取用户此刻的分工覆盖，之后整个节点沿用这份快照
    for chunk in graph.stream(first_input, cfg, stream_mode="updates"):
        check_cancelled()
        for node, update in chunk.items():
            if node == "__interrupt__":
                interrupt_payload = update[0].value
                continue
            now = time.monotonic()
            timeline.append({"node": node,
                             "at": datetime.now().isoformat(timespec="seconds"),
                             "dur_s": round(now - t0, 1)})
            t0 = now
            _record_progress(task, graph, cfg)
            persist(task)
        if boundary:
            boundary()
    check_cancelled()
    return interrupt_payload


def drive(task: dict, first_input, persist, checkpoint_db=None, on_saved=None) -> dict:
    """驱动图直到 完成 / 挂起等人 / 失败。直接修改并返回 task dict。

    first_input 三种形态（对应 LangGraph 的三种入口）：
    - dict（初始 state）：新任务从头跑；
    - Command(resume=...)：从挂起的 interrupt 续跑；
    - None：从上一个检查点继续（server 重启后恢复"interrupted"任务）。
    """
    # MEMORY_ENABLED=0 的运行时兜底（正常路径下 config import 时已生效）
    if os.getenv("MEMORY_ENABLED", "1") != "1":
        config.MEMORY_ENABLED = False

    from langgraph.checkpoint.sqlite import SqliteSaver
    db = str(checkpoint_db or config.CHECKPOINT_DB)
    Path(db).parent.mkdir(parents=True, exist_ok=True)
    cfg = {"configurable": {"thread_id": task["thread_id"]}, "recursion_limit": config.GRAPH_RECURSION_LIMIT}

    with _state_guard():
        check_cancelled()
        task["status"] = "running"
        task["save_started"] = False
        persist(task)
    feedback_token = save_gate_token = runtime_token = None
    from service.model_budget import budget_observer, observer_for
    cancellation = current_cancellation.get()
    state_lock = task_state_lock.get()
    account = observer_for(task, persist, lambda: state_lock or nullcontext()) if task.get("pipeline_version") == "v2" else None
    def observe_request(event, provider, elapsed):
        if event == "start":
            check_cancelled()
            if account:
                account(event, provider, elapsed)
            if cancellation:
                cancellation.request_started(lambda seconds: account("finish", provider, seconds) if account else None)
        else:
            if cancellation:
                cancellation.request_finished(elapsed)
            elif account:
                account(event, provider, elapsed)
            check_cancelled()
    budget_token = budget_observer.set(observe_request)
    # 专业节点分工：用户在页面/MCP 改的覆盖只在节点边界进入快照；每次实际接入记入 task["routes"] 供页面显示。
    from copy import deepcopy
    from ai_os_connection import use_role_settings
    roles = {"snapshot": {}}

    def refresh_roles():
        with _state_guard():
            roles["snapshot"] = deepcopy(task.get("role_settings", {}))

    def record_route(selected):
        with _state_guard():
            routes = task.setdefault("routes", [])
            routes.append({"role": selected.role, "provider": selected.provider, "model": selected.model,
                           "reason": str(selected.reason)[:300], "at": datetime.now().isoformat(timespec="seconds")})
            del routes[:-30]
            persist(task)
    if task.get("pipeline_version") == "v2":
        from pipeline_v2 import feedback_reader, save_gate
        feedback_token = feedback_reader.set(lambda: _feedback_snapshot(task))
        save_gate_token = save_gate.set(lambda state: _commit_save_gate(task, state, persist))
        from pipeline_v2 import runtime_settings_reader
        from service.jev_settings import runtime_state
        runtime_token = runtime_settings_reader.set(lambda: runtime_state(task["jev_settings"], task.get("topic_id", ""))
            if "jev_settings" in task else {})
    try:
        with use_role_settings(lambda: roles["snapshot"], record_route), SqliteSaver.from_conn_string(db) as saver:
            graph = graph_for(task, checkpointer=saver)
            payload = _stream_graph(task, graph, first_input, cfg, persist, boundary=refresh_roles)
            while payload is not None:
                _record_progress(task, graph, cfg)
                if task.get("pipeline_version", "v1") == "v1" and task.get("auto_approve") and payload["kind"] == "outline":
                    resume_value = AUTO_RESUME[payload["kind"]]
                else:
                    with _state_guard():
                        check_cancelled()
                        task["status"] = "awaiting_human"
                        task["interrupt"] = payload  # 大纲/成稿内容，等外部确认
                        persist(task)
                    return task
                payload = _stream_graph(task, graph, Command(resume=resume_value),
                                        cfg, persist, boundary=refresh_roles)
            _record_progress(task, graph, cfg)
            values = graph.get_state(cfg).values
            output_path = values.get("output_path", "")
            if not output_path:
                raise ValueError("流程结束但没有已保存稿件；请检查断点是否存在，不能标记完成")
            task["publication_ready"] = values.get("publication_ready", False)
            task["quality_issues"] = values.get("quality_issues", [])
            task["memory_result"] = values.get("memory_result", {})
    except TaskCancelled:
        task["status"] = "interrupted"
        task["error"] = "任务已取消；断点与已用模型预算保留"
        persist(task)
        return task
    except Exception as e:
        task["status"] = "failed"
        task["error"] = safe_error(e)
        persist(task)
        return task
    finally:
        budget_observer.reset(budget_token)
        if feedback_token is not None:
            feedback_reader.reset(feedback_token)
        if save_gate_token is not None:
            save_gate.reset(save_gate_token)
        if runtime_token is not None:
            runtime_settings_reader.reset(runtime_token)

    with _state_guard():
        check_cancelled()
        task["status"] = "completed"
        task["interrupt"] = None
        task["output_path"] = output_path
        persist(task)

    # 成稿已落盘（save 节点完成）→ git 快照。快照失败不影响成稿本身。
    if on_saved and task["output_path"]:
        try:
            task["snapshot_commit"] = on_saved(task)
        except Exception as e:
            task["snapshot_error"] = f"{type(e).__name__}: {e}"
        persist(task)
    return task


def revision_seed(base: dict) -> dict:
    """修订任务的起点：原稿作为现成稿件，大纲沿用原稿的标题结构，并标记待按作者要求修改。

    这样 AI OS 在摘要确认后派初稿节点在原文上定向修改，再走审核、润色、核验与终审，而不是从头重写。
    原稿按登记的哈希核对，文件被改动过就拒绝，免得在别人看不到的内容上修订。
    """
    import hashlib
    import re
    data = Path(base["path"]).read_bytes()
    if hashlib.sha256(data).hexdigest() != base["sha256"]:
        raise ValueError("修订起点的正文已变化，请重新从版本线选择")
    article = data.decode("utf-8")
    headings = [line.strip() for line in article.splitlines() if re.match(r"#{1,3} ", line)]
    outline = (f"沿用第 {base['version']} 版的结构定向修订，不重新组织全文：\n" + "\n".join(headings)
               if headings else f"沿用第 {base['version']} 版的结构定向修订，不重新组织全文。")
    return {"draft": article, "outline": outline, "research_completed": True, "revision_pending": True,
            "final_feedback": base["feedback"], "revision_base": {k: base[k] for k in ("version", "sha256", "path")}}


def start_task(task: dict, persist, checkpoint_db=None, on_saved=None) -> dict:
    """新任务：用 task 里的 topic/idea/thread_id 组装初始 state 从头跑；修订任务另带原稿起点。"""
    state = {**initial_state(task.get("topic", ""), task.get("idea", ""), task["thread_id"], task.get("topic_id", "")),
             "pipeline_version": task.get("pipeline_version", "v1"),
             "sample_requested": task.get("sample_requested", False)}
    if task.get("revision_base"):
        state.update(revision_seed(task["revision_base"]))
    return drive(task, state, persist, checkpoint_db, on_saved)


def resume_task(task: dict, decision: dict, persist, checkpoint_db=None,
                on_saved=None) -> dict:
    """挂起任务续跑：decision 就是 interrupt 的 resume 值（格式见 AUTO_RESUME）。"""
    task["interrupt"] = None
    return drive(task, Command(resume=decision), persist, checkpoint_db, on_saved)


def continue_task(task: dict, persist, checkpoint_db=None, on_saved=None) -> dict:
    """从上一个检查点继续（server 重启时任务正跑到一半、状态为 interrupted）。"""
    return drive(task, None, persist, checkpoint_db, on_saved)
