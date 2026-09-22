"""AI OS 终端界面：uv run python tui.py，支持 --mock 无网络演示。"""
from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path

from textual import on, work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Footer, Header, Input, Label, RichLog, Select, Static, TextArea


class ConnectionScreen(ModalScreen):
    """密钥仅保存在进程内；禁止把输入内容当普通聊天记录。"""
    def compose(self) -> ComposeResult:
        with Vertical(id="connection-form"):
            yield Label("AI OS 接入设置（API key 仅在本次进程内使用）")
            yield Select([("自动：Codex → Claude Code → DeepSeek", "auto"),
                          ("Codex（至少剩余15%）", "codex"),
                          ("Claude Code（至少剩余10%）", "claude"),
                          ("DeepSeek API", "deepseek"), ("OpenAI 兼容 API", "api")], value="auto", id="provider")
            yield Input(placeholder="API base URL（自定义 API 必填）", id="base-url")
            yield Input(placeholder="模型（自定义 API 必填）", id="model")
            yield Input(placeholder="API key（不显示、不落盘）", password=True, id="api-key")
            yield Static("自动模式使用本机 CLI 登录；额度未知时拒绝接入该 CLI。", id="connection-note")
            with Horizontal():
                yield Button("应用设置", id="apply-connection", variant="primary")
                yield Button("取消", id="cancel-connection")

    @on(Button.Pressed, "#apply-connection")
    def apply(self):
        from ai_os_connection import ConnectionSettings
        try:
            settings = ConnectionSettings(provider=str(self.query_one("#provider", Select).value),
                base_url=self.query_one("#base-url", Input).value.strip(),
                model=self.query_one("#model", Input).value.strip(),
                api_key=self.query_one("#api-key", Input).value.strip())
            self.dismiss(settings)
        except (ValueError, RuntimeError):
            self.query_one("#connection-note", Static).update("配置无效，请核对地址、模型和密钥。")

    @on(Button.Pressed, "#cancel-connection")
    def cancel(self):
        self.dismiss(None)


class RoleScreen(ModalScreen):
    """专业节点分工：每个角色可指定提供方/模型或恢复配置分工。下一节点边界生效，在途调用不变。"""
    PROVIDER_OPTIONS = [("沿用配置分工", ""), ("Claude Code", "claude"), ("Codex", "codex"), ("CodeBuddy", "codebuddy"),
                        ("DeepSeek API", "deepseek"), ("千问 API", "dashscope")]

    def __init__(self, snapshot):
        super().__init__()
        self.snapshot = dict(snapshot)

    def compose(self) -> ComposeResult:
        from agent_cli import display_name
        state = self.snapshot
        with Vertical(id="role-form"):
            yield Label("专业节点分工 · " + state["task_id"] + "（AI OS 自身接入在“接入设置”）")
            for role in ROLES[1:]:
                provider, model = state["config"].get(role, ("", ""))
                override = state["overrides"].get(role, {})
                with Horizontal(classes="role-row"):
                    yield Static(f"{role}\n配置：{display_name(provider)}" + (f" / {model}" if model else ""), classes="role-name")
                    yield Select(self.PROVIDER_OPTIONS, value=override.get("provider", ""), allow_blank=False,
                                 id=f"role-provider-{role}", classes="role-provider")
                    yield Input(value=override.get("model", ""), placeholder="模型（留空用 CLI 默认/回退默认）",
                                id=f"role-model-{role}", classes="role-model")
            recent = state.get("routes") or []
            yield Static("最近实际接入：" + ("；".join(
                f"{r.get('role')}→{display_name(r.get('provider', ''))}" + (f"/{r.get('model')}" if r.get('model') else "")
                for r in recent) if recent else "尚无") + "\n设置在下一节点边界生效；正在执行的调用沿用其开始时的分工。",
                id="role-note")
            with Horizontal():
                yield Button("应用分工", id="apply-roles", variant="primary")
                yield Button("取消", id="cancel-roles")

    @on(Button.Pressed, "#apply-roles")
    def apply(self):
        settings = {}
        for role in ROLES[1:]:
            provider = str(self.query_one(f"#role-provider-{role}", Select).value or "")
            model = self.query_one(f"#role-model-{role}", Input).value.strip()
            settings[role] = {"provider": provider, "model": model} if provider else None
        self.dismiss({"task_id": self.snapshot["task_id"], "settings": settings})

    @on(Button.Pressed, "#cancel-roles")
    def cancel(self):
        self.dismiss(None)


class BudgetScreen(ModalScreen):
    """只收集明确追加数值；绑定打开时任务，确认后也不自动续跑。"""
    def __init__(self, snapshot):
        super().__init__()
        self.snapshot = dict(snapshot)

    def compose(self) -> ComposeResult:
        state = self.snapshot
        with Vertical(id="budget-form"):
            yield Label("追加模型预算 · " + state["task_id"])
            yield Static(f"主题：{state['topic']}\n模型请求：{state['model_calls']} / {state['call_limit']} 次\n"
                         f"模型执行：{state['model_seconds']:.1f} / {state['seconds_limit']} 秒")
            yield Input(placeholder="明确增加请求次数（0至120）", id="budget-calls", type="integer")
            yield Input(placeholder="明确增加执行秒数（0至86400）", id="budget-seconds", type="integer")
            if state.get("paused"):
                # AI OS 因调度/修订预算暂停时，只有把追加值交回暂停点才能继续；这两项属于图内预算，
                # 与任务级的请求次数/秒数分开记录。
                yield Static(f"AI OS 已暂停：{state.get('pause_reason', '')}（已调度 {state.get('ai_os_steps', '?')} 步）")
                yield Input(placeholder="明确增加 AI OS 调度步数（0至100）", id="budget-steps", type="integer")
                yield Input(placeholder="明确增加每问题修订次数（0至10）", id="budget-revisions", type="integer")
                note = "至少填写一项非零数值。追加调度步数或修订次数会从暂停点继续 AI OS；只追加请求/秒数不会续跑。"
            else:
                note = "至少填写一项非零数值。追加后仍由你决定何时续跑或确认当前内容。"
            yield Static(note, id="budget-note")
            with Horizontal():
                yield Button("确认追加（步数/修订会续跑）" if state.get("paused") else "确认追加（不续跑）",
                             id="apply-budget", variant="warning", disabled=True)
                yield Button("取消", id="cancel-budget")

    def values(self):
        selectors = ["#budget-calls", "#budget-seconds"]
        if self.snapshot.get("paused"):
            selectors += ["#budget-steps", "#budget-revisions"]
        values = [self.query_one(selector, Input).value.strip() for selector in selectors]
        if any(value and not value.isdecimal() for value in values):
            raise ValueError()
        numbers = [int(value or "0") for value in values] + [0, 0]
        calls, seconds, steps, revisions = numbers[:4]
        if (not 0 <= calls <= 120 or not 0 <= seconds <= 86400 or not 0 <= steps <= 100
                or not 0 <= revisions <= 10 or not (calls or seconds or steps or revisions)):
            raise ValueError()
        return calls, seconds, steps, revisions

    @on(Input.Changed, "#budget-calls")
    @on(Input.Changed, "#budget-seconds")
    @on(Input.Changed, "#budget-steps")
    @on(Input.Changed, "#budget-revisions")
    def values_changed(self):
        # 无效提交不触发按钮动画，避免用户纠正输入后紧接的有效点击被吞掉。
        try:
            self.values()
            valid = True
        except ValueError:
            valid = False
        self.query_one("#apply-budget", Button).disabled = not valid

    @on(Button.Pressed, "#apply-budget")
    def apply(self):
        try:
            calls, seconds, steps, revisions = self.values()
        except ValueError:
            self.query_one("#budget-note", Static).update(
                "次数须为0至120、秒数须为0至86400、调度步数0至100、修订次数0至10的整数，至少一项非零；尚未追加。")
            return
        self.dismiss({"task_id": self.snapshot["task_id"], "calls": calls, "seconds": seconds,
                      "steps": steps, "revisions": revisions})

    @on(Button.Pressed, "#cancel-budget")
    def cancel(self):
        self.dismiss(None)


class WritingApp(App):
    TITLE = "写作管道 · AI OS"
    CSS = """
    #toolbar { height: 3; }
    #toolbar Button { min-width: 10; margin-right: 1; }
    #status { height: 3; padding: 0 1; background: $panel; }
    #chat { height: 1fr; border: solid $primary; }
    #message { height: 7; }
    #actions { height: 3; }
    #publish-actions { height: 3; }
    #topic { width: 1fr; }
    #history { width: 35; }
    #connection-form { width: 78; height: auto; padding: 2; background: $surface; border: thick $primary; }
    ConnectionScreen { align: center middle; }
    #connection-form Input, #connection-form Select { margin: 1 0; }
    #budget-form { width: 78; height: auto; padding: 2; background: $surface; border: thick $warning; }
    BudgetScreen { align: center middle; }
    #budget-form Input { margin: 1 0; }
    #budget-note { height: 3; }
    #budget-form Horizontal { height: 3; }
    #role-form { width: 110; height: auto; padding: 1 2; background: $surface; border: thick $primary; }
    RoleScreen { align: center middle; }
    .role-row { height: 3; }
    .role-name { width: 34; }
    .role-provider { width: 26; }
    .role-model { width: 1fr; }
    #role-note { height: 3; }
    #role-form > Horizontal { height: 3; }
    """
    BINDINGS = [("ctrl+enter", "send", "发送"), ("ctrl+q", "quit", "退出并保留断点")]

    def __init__(self, directory: Path, *, controller=None):
        super().__init__()
        from service.tui_controller import TuiController
        from ai_os_connection import ConnectionSettings
        self.controller = controller or TuiController(directory)
        self.directory = directory
        self._directory_guard = None
        self.settings = ConnectionSettings()
        self.marker = None
        self._action_pending = False
        self._quitting = False
        self._shutdown_checked = False
        self._history_ids = tuple(key for _, key in self.controller.history_options())
        self.route_note = "尚未接入：自动（Codex → Claude Code → DeepSeek API），每次调用按剩余额度决定"
        self._routes_shown = 0

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="toolbar"):
            yield Input(placeholder="文章主题", id="topic")
            yield Button("接入设置", id="connection")
            yield Button("模型分工", id="roles")
            yield Button("续跑", id="retry")
            yield Select(self.controller.history_options(), prompt="历史任务", id="history")
        yield Static(self.route_note, id="status")
        yield RichLog(id="chat", wrap=True, markup=False, highlight=False)
        yield TextArea(id="message")
        with Horizontal(id="actions"):
            yield Button("发送 / 开始", id="send", variant="primary")
            yield Button("确认当前摘要", id="approve", disabled=True, variant="success")
            yield Button("新文章", id="new")
            yield Checkbox("先确认短样稿", id="sample")
            yield Button("记录为本篇偏好", id="preference")
        with Horizontal(id="publish-actions"):
            yield Button("追加预算", id="budget", disabled=True)
            yield Button("发布预览", id="publish-preview")
            yield Button("确认发布", id="publish-confirm", disabled=True, variant="warning")
        yield Footer()

    def on_mount(self):
        from tools.storage import exclusive
        self._directory_guard = exclusive(self.directory / ".ui.lock")
        try:
            self._directory_guard.__enter__()
        except RuntimeError:
            self._directory_guard = None
            self.exit(message="这个TUI任务目录已在另一个终端打开；请使用原窗口或另选--directory。")
            return
        self.query_one("#chat", RichLog).write("AI OS：请输入主题和观点材料。我会先生成摘要，等待你确认，再开始写作。修改意见直接发送；发送消息本身不会表示通过。")
        self.query_one("#chat", RichLog).write("专业节点分工：" + describe_roles() + "（AI OS 接入另按额度决定；用 --role 可为本次启动显式改分工）")
        self.set_interval(0.5, self.refresh_status)
        self.query_one("#chat", RichLog).write("退出会保留已完成节点的断点；尚在执行的调用可能需要续跑重试。")

    async def action_quit(self):
        if self._quitting:
            return
        import asyncio
        self._quitting = True
        result = await asyncio.to_thread(self.controller.manager.shutdown, 8.0)
        if result["remaining_cli"]:
            self._quitting = False
            self.notify("仍在回收本任务CLI，已停止新请求；请稍后再次退出", severity="warning")
            return
        self._shutdown_checked = True
        self.exit()

    async def on_unmount(self):
        import asyncio
        await asyncio.to_thread(self.controller.manager.shutdown, 0.0 if self._shutdown_checked else 8.0)
        if self._directory_guard is not None:
            self._directory_guard.__exit__(None, None, None)
            self._directory_guard = None

    def route_event(self, selection):
        if self._quitting:
            return
        self.call_from_thread(self._show_route, selection)

    def _show_route(self, selection):
        # 状态栏立即更新；对话区的接入记录统一来自任务登记簿的 routes（含专业节点），避免同一次接入重复显示。
        from ai_os_connection import describe_selection
        self.route_note = f"AI OS 已接入 {describe_selection(selection)} · {selection.reason}"

    def _show_routes(self, view, status):
        routes = status.get("routes") or []
        if self.marker is not None and self.marker[0] != status.get("task_id"):
            self._routes_shown = 0
        if len(routes) < self._routes_shown:
            self._routes_shown = 0
        fresh = routes[self._routes_shown:]
        if not fresh:
            return
        from agent_cli import display_name
        chat = view.query_one("#chat", RichLog)
        for route in fresh:
            role = route.get("role") or "节点"
            label = "AI OS" if role == "orchestrator" else role
            chat.write(f"{label} 已接入 {display_name(route.get('provider', ''))}"
                       + (f" / {route['model']}" if route.get("model") else "") + f" · {route.get('reason', '')}")
        latest = routes[-1]
        self.route_note = (("AI OS" if latest.get("role") == "orchestrator" else latest.get("role", "")) + " 已接入 "
                           + display_name(latest.get("provider", "")) + (f" / {latest['model']}" if latest.get("model") else ""))
        self._routes_shown = len(routes)

    def refresh_status(self):
        from textual.css.query import NoMatches
        try:
            self._refresh_status()
        except NoMatches:
            # 定时器可能在主屏幕尚未挂载完或正在退出时触发；此时没有可刷新的控件，下一次定时再刷。
            return

    def _refresh_status(self):
        # 模态表单在屏幕栈顶部，状态栏仍属于主屏幕，不能向表单查询其控件。
        if self._quitting or not self.screen_stack:
            return
        view = self.screen_stack[0]
        options = self.controller.history_options()
        identities = tuple(key for _, key in options)
        if identities != self._history_ids:
            self._history_ids = identities
            history = view.query_one("#history", Select)
            history.set_options(options)
        status = self.controller.status()
        view.query_one("#budget", Button).disabled = bool(self._action_pending or
            status.get("pipeline_version") != "v2" or
            status.get("status") not in {"failed", "interrupted", "awaiting_human"})
        if not status:
            return
        self._show_routes(view, status)
        heartbeat = status.get("heartbeat") or {}
        line = f"{status['task_id']} · {status['status']} · {self.route_note}"
        if heartbeat:
            line += f" · {heartbeat.get('role', '')} 已运行 {heartbeat.get('elapsed_s', '?')} 秒"
            if heartbeat.get("progress_source") == "cli":
                line += f" · CLI事件 {heartbeat.get('events', 0)} 个 · 可见文本 {heartbeat.get('content_chars', 0)} 字"
            elif heartbeat.get("phase") == "tool_wait":
                line += " · 等待工具返回"
        view.query_one("#status", Static).update(line)
        payload = status.get("interrupt")
        marker = (status.get("task_id"), status.get("status"), json.dumps(payload, ensure_ascii=False), str(status.get("timeline")))
        if marker == self.marker:
            return
        self.marker = marker
        button = view.query_one("#approve", Button)
        button.disabled = True
        chat = view.query_one("#chat", RichLog)
        if payload:
            self.controller.show_interrupt(payload)
            kind = payload.get("kind")
            if kind == "sample":
                text = "短样稿：请回复 A / B 选择，或直接提出修改意见。\n" + "\n\n".join(
                    str(k) + "：\n" + str(v) for k, v in payload.get("options", {}).items())
            elif kind == "final":
                # 稿首/稿尾的“润色说明/修改说明”注释是节点间的工作记录，保存时会被剥离，不属于稿件正文。
                text = re.sub(r"<!--.*?-->", "", payload.get("polished", ""), flags=re.S)
                text = re.sub(r"\n{3,}", "\n\n", text).strip()
                text = (f"第 {payload.get('article_version', '?')} 版稿件（摘要 v{payload.get('summary_version', '?')}）"
                        "已通过原意、事实、阅读质量审核和成稿核验。确认署名后保存本地；发布另行确认。\n\n" + text)
            else:
                text = payload.get("question") or payload.get("summary") or json.dumps(payload, ensure_ascii=False, indent=2)
            chat.write("AI OS · 待你确认\n" + str(text))
            if kind in {"summary", "final"}:
                button.label = "确认当前摘要" if kind == "summary" else "确认署名并保存本地"
                button.disabled = False
        elif status.get("status") == "failed":
            chat.write("运行失败，断点已保留：" + status.get("error", ""))
        elif status.get("status") == "completed":
            chat.write("已按你的终审确认保存本地：" + status.get("output_path", "") + "\n发布仍需单独预览和明确授权。")
        elif status.get("timeline"):
            chat.write("已完成节点：" + status["timeline"][-1]["node"])

    def submit_action(self, action, *args):
        task_id, displayed = self.controller.action_snapshot()
        self.submit_bound_action(action, args, task_id, displayed)

    def submit_bound_action(self, action, args, task_id, displayed):
        if self._quitting:
            self.notify("正在退出并回收当前任务，不能发起新操作", severity="warning")
            return
        if self._action_pending:
            self.notify("上一项操作尚未完成，请稍候", severity="warning")
            return
        self._action_pending = True
        self.perform(action, args, task_id, displayed, self.settings)

    @work(thread=True, group="user-action")
    def perform(self, action, args, task_id, displayed, settings):
        from ai_os_connection import use_connection
        try:
            with use_connection(settings, on_route=self.route_event):
                result = self.controller.execute(action, args, task_id, displayed)
            self.call_from_thread(self.show_action_result, action, result)
            self.call_from_thread(self.refresh_status)
        except Exception as exc:
            from service.runner import safe_error
            self.call_from_thread(self.action_failed, safe_error(exc, (settings.api_key,)))
        finally:
            self.call_from_thread(self.action_finished)

    def action_failed(self, message):
        self.screen_stack[0].query_one("#chat", RichLog).write("未执行：" + message)

    def action_finished(self):
        self._action_pending = False

    def show_action_result(self, action, result):
        chat = self.query_one("#chat", RichLog)
        if action == "preview":
            plan = result["plan"]
            chat.write("发布预览（仍未发布）：\n" + "\n".join(
                f"{key}：{plan[key]}" for key in ("repository", "remote_url", "branch", "destination", "article_sha256"))
                + "\n\n完整正文：\n" + result["article"])
            self.controller.show_publish_preview(result)
            self.query_one("#publish-confirm", Button).disabled = False
        elif action == "publish":
            chat.write(f"任务 {result['task_id']} 发布结果：{result.get('status')}；网站部署状态：{result.get('deployment_status', '未核验')}")
            self.query_one("#publish-confirm", Button).disabled = True
        elif action == "preference":
            chat.write("已记录为本篇用户偏好。")
        elif action == "configure_roles":
            from agent_cli import display_name
            overrides = result.get("role_settings") or {}
            described = "；".join(f"{role}={display_name(v['provider'])}" + (f" / {v['model']}" if v.get("model") else "")
                                 for role, v in overrides.items()) or "全部沿用配置分工"
            chat.write(f"任务 {result['task_id']} 的专业节点分工已更新：{described}。{result.get('applies', '')}。"
                       "实际接入以对话区“已接入”记录为准。")
        elif action == "extend_budget":
            line = f"任务 {result['task_id']} 已追加 {result['added_calls']} 次模型请求、{result['added_seconds']} 秒模型执行预算"
            if result.get("added_steps") or result.get("added_revisions"):
                line += f"、{result.get('added_steps', 0)} 步 AI OS 调度、{result.get('added_revisions', 0)} 次每问题修订"
            if result.get("resumed"):
                chat.write(line + "。AI OS 已从暂停点继续；这不构成对摘要、短样稿或最终稿的确认。")
            else:
                chat.write(line + "。尚未续跑；失败或暂停任务请点击“续跑”，待确认任务请继续回应所展示的内容。")

    @on(TextArea.Changed, "#message")
    def message_changed(self):
        self.controller.invalidate_publish_preview()
        self.query_one("#publish-confirm", Button).disabled = True

    @on(Input.Changed, "#topic")
    def topic_changed(self):
        self.controller.invalidate_publish_preview()
        self.query_one("#publish-confirm", Button).disabled = True

    def action_send(self):
        if self._action_pending:
            return
        text = self.query_one("#message", TextArea).text.strip()
        if not text:
            return
        self.query_one("#chat", RichLog).write("你：" + text)
        self.query_one("#message", TextArea).clear()
        if not self.controller.task_id:
            self.submit_action("start", self.query_one("#topic", Input).value, text,
                         self.query_one("#sample", Checkbox).value)
        else:
            self.submit_action("send", text)

    @on(Button.Pressed)
    def buttons(self, event):
        key = event.button.id
        if key == "send":
            self.action_send()
        elif key == "approve":
            self.submit_action("approve")
        elif key == "retry":
            self.submit_action("retry")
        elif key == "connection":
            self.push_screen(ConnectionScreen(), self.connection_changed)
        elif key == "roles":
            try:
                self.push_screen(RoleScreen(self.controller.role_snapshot()), self.roles_changed)
            except ValueError as exc:
                self.notify(str(exc), severity="warning")
        elif key == "budget":
            if self._action_pending:
                self.notify("上一项操作尚未完成，请稍候", severity="warning")
            else:
                try:
                    self.push_screen(BudgetScreen(self.controller.budget_snapshot()), self.budget_changed)
                except ValueError as exc:
                    self.notify(str(exc), severity="warning")
        elif key == "preference":
            text = self.query_one("#message", TextArea).text.strip()
            if text:
                self.submit_action("preference", text)
                self.query_one("#message", TextArea).clear()
        elif key == "publish-preview":
            self.submit_action("preview")
        elif key == "publish-confirm":
            self.submit_action("publish")
        elif key == "new":
            if self._action_pending or self.controller.status().get("status") in {"pending", "running"}:
                self.notify("当前任务仍在运行", severity="warning")
            else:
                self.controller.clear()
                self.marker = None
                self.query_one("#approve", Button).disabled = True
                self.query_one("#publish-confirm", Button).disabled = True
                self.query_one("#budget", Button).disabled = True

    def roles_changed(self, decision):
        if decision is not None:
            self.submit_bound_action("configure_roles", (decision["settings"],), decision["task_id"], None)

    def budget_changed(self, decision):
        if decision is not None:
            # 不用当前选择重新取task_id；后台execute会拒绝表单打开后切换任务。
            self.submit_bound_action("extend_budget", (decision["calls"], decision["seconds"],
                                                       decision.get("steps", 0), decision.get("revisions", 0)),
                                     decision["task_id"], None)

    def connection_changed(self, settings):
        if settings:
            self.settings = settings
            from ai_os_connection import describe_settings
            self.route_note = f"已选择 {describe_settings(settings)}；尚未接入，下一次调用时检查额度"
            self.query_one("#chat", RichLog).write(self.route_note)

    @on(Select.Changed, "#history")
    def select_history(self, event):
        if event.value != Select.BLANK:
            self.controller.select(str(event.value))
            self.query_one("#publish-confirm", Button).disabled = True
            self.marker = None
            self.refresh_status()


ROLES = ("orchestrator", "architect", "researcher", "writer", "reviewer", "stylist", "final_check")
PROVIDERS = ("claude", "codex", "codebuddy", "deepseek", "dashscope")


def describe_roles() -> str:
    import config
    from agent_cli import display_name
    return "；".join(f"{role}={display_name(provider)}" + (f" / {model}" if model else "")
                     for role, (provider, model) in config.ROLE_MODELS.items() if role != "orchestrator")


def apply_role_overrides(specs, environ=os.environ):
    """--role 只影响本次进程：写入 config 读取的同名环境变量，不改 config.py 默认分工，不落任务记录。"""
    for spec in specs:
        role, _, target = spec.partition("=")
        provider, _, model = target.partition(":")
        if role not in ROLES or provider not in PROVIDERS:
            raise SystemExit(f"--role 格式为 角色=提供方[:模型]；角色取 {', '.join(ROLES)}，提供方取 {', '.join(PROVIDERS)}：{spec}")
        environ[f"WRITING_{role.upper()}_PROVIDER"] = provider
        environ[f"WRITING_{role.upper()}_MODEL"] = model


def main():
    parser = argparse.ArgumentParser(description="AI OS 终端对话；默认按额度自动接入 Codex → Claude Code → DeepSeek API")
    parser.add_argument("--mock", action="store_true", help="模拟模式，不发送真实模型请求")
    parser.add_argument("--directory", type=Path,
                        default=Path(os.getenv("WRITING_RUNTIME_DIR") or Path(__file__).parent / ".runtime") / "tui",
                        help="任务目录（含 tasks.json 与 checkpoints.sqlite）；默认 .runtime/tui，可指向验收运行目录或 .runtime/service")
    parser.add_argument("--role", action="append", default=[], metavar="角色=提供方[:模型]",
                        help="仅本次启动显式改某个专业节点的模型，如 reviewer=claude:claude-fable-5-1；可重复")
    args = parser.parse_args()
    if args.mock:
        os.environ.update(MOCK_LLM="1", MEMORY_ENABLED="0")
    apply_role_overrides(args.role)
    WritingApp(args.directory).run()


if __name__ == "__main__":
    main()
