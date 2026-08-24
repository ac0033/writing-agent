"""写作图的自动驱动循环（service 层核心）。

职责：在给定 thread_id 上跑 LangGraph 图，处理 human_outline / human_final
两个人工确认节点的 interrupt：

- auto_approve=True：outline 自动 resume {"approved": True, "feedback": ""}；
  final 自动 resume {"route": "approve", "feedback": ""}，全程无人跑到 save。
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
from datetime import datetime

from langgraph.types import Command

import config
from graph import build_graph

# resume 值的格式与 main.py handle_interrupt 保持一致（graph.py:266/445 的契约）
AUTO_RESUME = {
    "outline": {"approved": True, "feedback": ""},
    "final": {"route": "approve", "feedback": ""},
}


def initial_state(topic: str, idea: str, thread_id: str) -> dict:
    """图的初始 state，字段照抄 main.py 的启动逻辑（main.py:235-241）。"""
    return {"topic": topic, "user_idea": idea, "thread_id": thread_id,
            "outline_feedback": [], "materials": [], "review_cycles": 0,
            "research_rounds": 0, "thinking_log": []}


def _record_progress(task: dict, graph, cfg: dict) -> None:
    """从检查点状态提取进度：已完成的 LLM 节点（thinking_log）+ 下一步节点。"""
    try:
        st = graph.get_state(cfg)
        task["progress"] = [e.get("node", "?")
                            for e in st.values.get("thinking_log", [])]
        task["next_nodes"] = list(st.next)
    except Exception:
        pass  # 进度只是观测信息，拿不到不影响主流程


def _stream_graph(task: dict, graph, first_input, cfg: dict, persist) -> dict | None:
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
    for chunk in graph.stream(first_input, cfg, stream_mode="updates"):
        for node, update in chunk.items():
            if node == "__interrupt__":
                interrupt_payload = update[0].value
                continue
            now = time.monotonic()
            timeline.append({"node": node,
                             "at": datetime.now().isoformat(timespec="seconds"),
                             "dur_s": round(now - t0, 1)})
            t0 = now
            persist(task)
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
    cfg = {"configurable": {"thread_id": task["thread_id"]}}

    task["status"] = "running"
    persist(task)
    try:
        with SqliteSaver.from_conn_string(db) as saver:
            graph = build_graph(checkpointer=saver)
            payload = _stream_graph(task, graph, first_input, cfg, persist)
            while payload is not None:
                _record_progress(task, graph, cfg)
                if task.get("auto_approve"):
                    resume_value = AUTO_RESUME[payload["kind"]]
                else:
                    task["status"] = "awaiting_human"
                    task["interrupt"] = payload  # 大纲/成稿内容，等外部确认
                    persist(task)
                    return task
                payload = _stream_graph(task, graph, Command(resume=resume_value),
                                        cfg, persist)
            _record_progress(task, graph, cfg)
            output_path = graph.get_state(cfg).values.get("output_path", "")
    except Exception as e:
        task["status"] = "failed"
        task["error"] = f"{type(e).__name__}: {e}"
        persist(task)
        return task

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


def start_task(task: dict, persist, checkpoint_db=None, on_saved=None) -> dict:
    """新任务：用 task 里的 topic/idea/thread_id 组装初始 state 从头跑。"""
    return drive(task,
                 initial_state(task.get("topic", ""), task.get("idea", ""),
                               task["thread_id"]),
                 persist, checkpoint_db, on_saved)


def resume_task(task: dict, decision: dict, persist, checkpoint_db=None,
                on_saved=None) -> dict:
    """挂起任务续跑：decision 就是 interrupt 的 resume 值（格式见 AUTO_RESUME）。"""
    task["interrupt"] = None
    return drive(task, Command(resume=decision), persist, checkpoint_db, on_saved)


def continue_task(task: dict, persist, checkpoint_db=None, on_saved=None) -> dict:
    """从上一个检查点继续（server 重启时任务正跑到一半、状态为 interrupted）。"""
    return drive(task, None, persist, checkpoint_db, on_saved)
