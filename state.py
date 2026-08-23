"""LangGraph 全局状态定义。

所有节点共享这一个 State。节点返回的 dict 会合并进 State；
带 Annotated reducer 的字段（如 list 追加）按 reducer 规则合并，其余直接覆盖。
"""
from typing import Annotated, TypedDict

from operator import add


class Material(TypedDict):
    """一份资料：内容 + 来源。来源必须非空，这是 agent3 的硬要求。"""
    title: str
    content: str
    source_url: str


class WritingState(TypedDict, total=False):
    # ---- 用户输入 ----
    topic: str                # 文章主题
    user_idea: str            # 用户的思路/方向/想法
    thread_id: str            # 会话 id（main.py 生成；记忆服务 session_end 的 session_id）

    # ---- agent1：定框架 ----
    outline: str              # 大纲 + 结构 + 写作思路
    research_brief: str       # agent1 给 agent3 的资料需求清单
    outline_feedback: Annotated[list[str], add]  # 用户对大纲的历轮反馈
    outline_approved: bool

    # ---- agent3：资料 ----
    materials: Annotated[list[Material], add]  # 资料库，追加合并
    research_request: str     # agent2 发来的补充资料请求（空 = 按 brief 搜集）
    research_rounds: int      # 补充资料轮数

    # ---- agent2：初稿 ----
    draft: str
    needs_research: bool      # 初稿节点是否发出了补充资料请求

    # ---- agent4：审核 ----
    review_verdict: str       # "pass" / "fail"
    review_comments: str      # 审核意见（fail 时是重写依据；强制放行时随稿下传）
    review_cycles: int
    forced_pass: bool         # 达到循环上限被默认放行

    # ---- agent5：润色 ----
    polished: str

    # ---- 过程留痕 ----
    thinking_log: Annotated[list[dict], add]  # 每节点一条 {node, model, thinking}

    # ---- 最终人工确认 ----
    final_route: str          # "approve" / "content" / "style"
    final_feedback: str
    final_article: str
    output_path: str
