"""CLI 入口。

用法：
    uv run python main.py                # 正常跑一次写作流程
    uv run python main.py --list         # 列出历史会话（thread-id / 主题 / 状态 / 稿子路径）
    uv run python main.py --push         # 结束后推送到博客仓库（需配置 BLOG_REPO_PATH）
    uv run python main.py --thread-id xx # 回到指定会话：断点续跑；已跑完的会话会重新进入终审环节
    MOCK_LLM=1 uv run python main.py     # mock 模式，不发真实 API 请求，用于测试流程
"""
import argparse
import os
import sys
import uuid

# --mock 要在 import config/graph 之前生效
if "--mock" in sys.argv:
    os.environ["MOCK_LLM"] = "1"

import config
from graph import build_graph


def read_multiline(prompt: str) -> str:
    print(prompt)
    print("（可多行输入，单独一行输入 END 结束）")
    lines = []
    while True:
        line = input()
        if line.strip() == "END":
            break
        lines.append(line)
    return "\n".join(lines).strip()


def expand_file_refs(text: str) -> str:
    """把 @引用 展开成实际内容，拼进输入一起给 LLM。支持两种引用：

    - @文件名.后缀（如 @agent的演变层次.md）→ 读取该文件全文；
    - @目录名（无后缀，如 @topic 或 @topic/blog1）→ 递归读取目录下全部文本文件。

    输入格式不设限，只依赖两条规则区分：
    - 名字里有 ".后缀" → 按文件处理（文件名里允许带空格，匹配到后缀名为止）；
    - 名字里没有 ".后缀" → 按目录处理（目录名不能含空格，遇到空格/标点即认为名字结束）。
    先在项目根目录按相对路径找，找不到再递归按名字搜（跳过 .venv 等目录）。
    找不到的引用保留原样并打印警告，不静默丢弃。
    """
    import re
    from pathlib import Path
    SKIP_DIRS = {".venv", ".git", "__pycache__", "node_modules", ".kimi-code"}
    # 无法按文本读取的格式，读到时跳过并提示（如 PDF 请先转成 md/txt 再引用）
    BINARY_EXTS = {".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico",
                   ".zip", ".gz", ".tar", ".7z", ".pyc", ".sqlite", ".exe", ".dll",
                   ".mp3", ".mp4", ".mov", ".woff", ".woff2"}

    # 文件：@ 后任意字符（可含空格、标点），懒惰匹配到第一个 ".后缀" 为止
    FILE_REF = r"@([^\s@][^@]*?\.[A-Za-z0-9]+)"
    # 目录：@ 后到第一个空格/标点为止的一段（不含 @）
    DIR_REF = r"@([^\s@，。；、,;:!?！？：)）\]】\"'’”`]+)"

    def resolve(name: str) -> Path | None:
        p = config.BASE_DIR / name
        if p.exists():
            return p
        hits = [h for h in config.BASE_DIR.rglob(name)
                if not any(part in SKIP_DIRS for part in h.parts)]
        return hits[0] if hits else None

    def read_file(p: Path) -> str | None:
        """读取单个文件并包装成引用块；二进制文件跳过返回 None。"""
        if p.suffix.lower() in BINARY_EXTS:
            print(f"⚠️ 跳过二进制文件（无法作为文本引用）：{p.name}")
            return None
        content = p.read_text(encoding="utf-8", errors="replace")
        display = p.relative_to(config.BASE_DIR) if p.is_relative_to(config.BASE_DIR) else p
        print(f"📎 已读取：{display}（{len(content)} 字）")
        return f"\n【引用文件：{p.name}】\n{content}\n【引用文件结束】\n"

    def repl(m: re.Match) -> str:
        name = m.group(1) or m.group(2)
        p = resolve(name)
        if p is None:
            print(f"⚠️ 引用未找到：{name}（保留原文，请检查文件名/目录名）")
            return m.group(0)
        if p.is_dir():
            files = sorted(f for f in p.rglob("*") if f.is_file()
                           and not any(part in SKIP_DIRS for part in f.parts))
            blocks = [b for f in files if (b := read_file(f)) is not None]
            print(f"📂 目录 {name}：读取了 {len(blocks)}/{len(files)} 个文件")
            return "".join(blocks) if blocks else m.group(0)
        block = read_file(p)
        return block if block is not None else m.group(0)

    return re.sub(FILE_REF + "|" + DIR_REF, repl, text)


def print_box(title: str, body: str) -> None:
    print("\n" + "=" * 70)
    print(f"  {title}")
    print("=" * 70)
    print(body)
    print("=" * 70 + "\n")


def load_sessions() -> dict:
    if config.SESSIONS_FILE.exists():
        import json
        return json.loads(config.SESSIONS_FILE.read_text(encoding="utf-8"))
    return {}


def save_session(thread_id: str, **fields) -> None:
    """登记/更新会话条目（sessions.json），让每个 thread_id 可查可回访。"""
    import json
    from datetime import datetime
    sessions = load_sessions()
    entry = sessions.get(thread_id, {})
    entry.update(fields)
    entry["updated_at"] = datetime.now().isoformat(timespec="seconds")
    sessions[thread_id] = entry
    config.SESSIONS_FILE.write_text(
        json.dumps(sessions, ensure_ascii=False, indent=2), encoding="utf-8")


def list_sessions() -> None:
    sessions = load_sessions()
    if not sessions:
        print("还没有任何会话记录。")
        return
    print(f"共 {len(sessions)} 个会话（用 --thread-id <id> 回到某个会话）：\n")
    for tid, s in sorted(sessions.items(),
                         key=lambda kv: kv[1].get("updated_at", ""), reverse=True):
        print(f"  {tid}  {s.get('updated_at', '?')}  [{s.get('status', '?')}]  {s.get('topic', '?')}")
        if s.get("output_path"):
            print(f"    稿子：{s['output_path']}")


def handle_interrupt(payload: dict, review_mode: bool = False) -> dict | None:
    """根据 interrupt 类型问用户，返回 resume 值。

    review_mode=True 用于"回到已跑完的会话"时的终审：1 表示退出（只查看不修改，
    返回 None）；2/3 与正常流程相同。review_mode=False（正常流程）时 1 = 通过保存。
    """
    if payload["kind"] == "outline":
        print_box("Agent1 产出的文章框架", payload["outline"])
        if payload.get("research_brief"):
            print_box("资料需求清单（将交给 Agent3 搜集）", payload["research_brief"])
        ans = input("框架是否通过？直接回车/y = 通过；否则输入你的修改意见（可用 @文件名 或 @目录名 引用材料）：\n> ").strip()
        if ans in ("", "y", "Y", "通过"):
            return {"approved": True, "feedback": ""}
        return {"approved": False, "feedback": expand_file_refs(ans)}

    if payload["kind"] == "final":
        print_box("Agent5 润色后的最终稿", payload["polished"])
        if payload.get("forced_pass"):
            print_box(
                "⚠️ 审核超限，尚未通过，请重点检查这些点",
                payload["review_comments"],
            )
        if payload.get("quality_issues"):
            print_box("发布前须解决的问题", "\n".join(payload["quality_issues"]))
        if review_mode:
            print("该会话已保存过。请选择：")
            print("  1 = 退出（本轮只查看，不做修改）")
            print("  2 = 内容有问题，回 Agent2 重写")
            print("  3 = 风格/表述有问题，回 Agent5 重润色")
        else:
            print("请选择：")
            print("  1 = 我已确认文章无误，保存本地（不发布）")
            print("  2 = 内容有问题，回 Agent2 重写")
            print("  3 = 风格/表述有问题，回 Agent5 重润色")
        choice = input("> ").strip()
        if choice == "1":
            return None if review_mode else {"route": "approve", "feedback": ""}
        route = "content" if choice == "2" else "style"
        feedback = read_multiline("请输入具体反馈（可用 @文件名 或 @目录名 引用材料）：")
        return {"route": route, "feedback": expand_file_refs(feedback)}

    raise ValueError(f"未知的 interrupt 类型：{payload}")


def maybe_push(output_path: str) -> dict | None:
    from tools.publishing import preview, publish
    try:
        plan = preview(output_path)
        print_box("稿件已保存本地，以下是发布预览", "\n".join(f"{k}: {v}" for k, v in plan.items() if k != "approval_token"))
        answer = input("是否将这个已保存版本发布到上述仓库？输入 发布 确认；回车保留本地：\n> ").strip()
        if answer != "发布":
            print("稿件保留本地，未发布。")
            return None
        result = publish(output_path, confirmed=True, approval_token=plan["approval_token"])
        print("已推送 GitHub；站点部署结果需另行核实。")
        return result
    except (ValueError, OSError, RuntimeError) as exc:
        print(f"发布未完成（本地稿件保留）：{exc}")
        return None


def main() -> None:
    parser = argparse.ArgumentParser(description="写作 Agent 工作流")
    parser.add_argument("--mock", action="store_true", help="不发真实 API 请求，测试流程用")
    parser.add_argument("--push", action="store_true", help="保存后展示发布预览，仍需单独确认")
    parser.add_argument("--topic-id", default="", help="同一主题跨文章复用的稳定标识")
    parser.add_argument("--topic-file", default="", help="使用 topic/ 内已保存的聊天金字塔主题文件新建文章")
    parser.add_argument("--thread-id", default=None,
                        help="会话 id；不填则新建。填已有 id：未完成=断点续跑，已完成=回到终审环节查看/修改")
    parser.add_argument("--list", action="store_true", help="列出历史会话（thread-id / 主题 / 状态 / 稿子路径）")
    args = parser.parse_args()
    if args.topic_file and args.thread_id:
        parser.error("--topic-file 用于新建；续跑旧会话只用 --thread-id，避免替换存档输入")
    prepared = None
    if args.topic_file:
        from tools.topics import load
        prepared = load(args.topic_file)
        if args.topic_id and args.topic_id != prepared["topic_id"]:
            parser.error("--topic-id 与主题文件不一致")

    if args.list:
        list_sessions()
        return

    thread_id = args.thread_id or uuid.uuid4().hex[:8]
    print(f"会话 id：{thread_id}（中断后可用 --thread-id {thread_id} 续跑，--list 查看全部历史会话）")

    # 记忆服务探测：不在线就明说，让"记忆没生效"可见而不是默默降级
    if config.MEMORY_ENABLED:
        from tools import memory
        if memory.available():
            print(f"记忆服务已连接：{config.MEMORY_MCP_URL}（记忆按主题隔离）")
        else:
            print(f"⚠️ 记忆服务未连接（{config.MEMORY_MCP_URL}），本次写作不注入记忆、"
                  "流程不受影响。启动方式见 agent-memory 仓库的 scripts/start_http_server.cmd")

    from langgraph.checkpoint.sqlite import SqliteSaver
    from langgraph.types import Command

    with SqliteSaver.from_conn_string(str(config.CHECKPOINT_DB)) as saver:
        graph = build_graph(checkpointer=saver)
        cfg = {"configurable": {"thread_id": thread_id}, "recursion_limit": config.GRAPH_RECURSION_LIMIT}

        # 已有存档 = 续跑，不再收集输入
        if saver.get_tuple(cfg) is None:
            from datetime import datetime
            topic = prepared["topic"] if prepared else input("文章主题：\n> ").strip()
            idea = prepared["idea"] if prepared else expand_file_refs(read_multiline(
                "你的思路/方向/想法（用 @文件名.后缀 引用单个文件、@目录名 引用整个目录，内容会自动读取拼入）："
            ))
            from tools.identity import topic_id
            identity = prepared["topic_id"] if prepared else topic_id(topic, args.topic_id)
            save_session(thread_id, topic=topic, topic_id=identity, status="进行中",
                         topic_file=prepared["topic_file"] if prepared else "",
                         topic_sha256=prepared["topic_sha256"] if prepared else "",
                         started_at=datetime.now().isoformat(timespec="seconds"))
            result = graph.invoke(
                {"topic": topic, "topic_id": identity, "user_idea": idea, "thread_id": thread_id,
                 "outline_feedback": [],
                 "materials": [], "review_cycles": 0, "research_rounds": 0,
                 "thinking_log": []},
                cfg,
            )
        else:
            print("检测到该会话已有存档，从断点继续。")
            st = graph.get_state(cfg)
            if not st.next:
                # 已跑完的会话：把状态拨回 stylist 刚完成的时刻，重新进入终审环节。
                # 这次终审按"查看模式"处理：1 = 退出不修改，2/3 = 回炉重写/重润色
                print("该会话已跑完，重新进入终审环节（可查看稿子，也可选择回炉修改）。")
                graph.update_state(cfg, {}, as_node="final_check")
                result = graph.invoke(None, cfg)
                resume_value = handle_interrupt(result["__interrupt__"][0].value,
                                                review_mode=True)
                if resume_value is None:
                    print("已退出，未做修改。")
                    return
                result = graph.invoke(Command(resume=resume_value), cfg)
            else:
                pending = [i.value for t in st.tasks if t.interrupts for i in t.interrupts]
                if pending:
                    # 断在人工确认点：直接处理这个 interrupt。
                    # 特殊情况：上次回访已完成的会话时选了"退出不修改"，流程会停在
                    # 终审 interrupt 上——这次再进来仍按查看模式处理（1 = 退出），
                    # 而不是显示"1 = 通过保存发布"让稿子被无意义地重复保存。
                    # 判断依据是 sessions.json 里该会话已登记为"已完成"。
                    review = (pending[0].get("kind") == "final"
                              and load_sessions().get(thread_id, {}).get("status") == "已完成")
                    resume_value = handle_interrupt(pending[0], review_mode=review)
                    if resume_value is None:
                        print("已退出，未做修改。")
                        return
                    result = graph.invoke(Command(resume=resume_value), cfg)
                else:
                    # 断在两个节点之间（比如中途 Ctrl+C）：从存档继续跑
                    result = graph.invoke(None, cfg)

        while "__interrupt__" in result:
            payload = result["__interrupt__"][0].value
            resume_value = handle_interrupt(payload)
            result = graph.invoke(Command(resume=resume_value), cfg)

    output_path = result.get("output_path")
    print(f"\n完成！稿子已保存：{output_path}")
    if output_path:
        save_session(thread_id, status="已完成", output_path=output_path)
    if output_path and (args.push or (config.BLOG_REPO_PATH and not config.MOCK_LLM)):
        maybe_push(output_path)


if __name__ == "__main__":
    main()
