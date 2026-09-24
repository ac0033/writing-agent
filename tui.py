"""AI OS 终端界面：uv run python tui.py，支持 --mock 无网络演示。"""
from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (Button, Checkbox, Input, Label, Markdown, OptionList, RadioButton, RadioSet, Select,
                             Static, TextArea)
from textual.widgets.option_list import Option


class ConnectionScreen(ModalScreen):
    """AI OS 接入页，分两步：先选接入方式，再从该方式的可选模型里选。

    启动时作为开始页出现（不能取消）；之后从顶栏“接入”打开。第 1 步先检测本机 Codex / Claude Code，
    没装的不可选。“自动”只在本机私有设置开启时出现（config.AI_OS_AUTO_ENABLED）。
    密钥只随本进程；选择（不含密钥）记在任务目录供下次预选。
    """
    # 接入页只有选择框和少量输入框，Ctrl+C 不需要承担复制，按终端习惯直接退出。
    BINDINGS = [Binding("escape", "close", "关闭", show=False),
                Binding("ctrl+c", "app.quit", "退出", show=False, priority=True)]
    PROVIDERS = (("auto", "自动"), ("codex", "Codex"), ("claude", "Claude Code"), ("deepseek", "DeepSeek API"),
                 ("api", "OpenAI 兼容 API"), ("anthropic", "Anthropic 兼容 API"))
    DEFAULT, MANUAL = "__default__", "__manual__"

    def __init__(self, *, initial=False, remembered=None, clis=None, detector=None, allow_auto=None):
        super().__init__()
        import config
        self.initial = initial
        self.remembered = dict(remembered or {})
        self.clis = clis
        self.detector = detector
        self.allow_auto = config.AI_OS_AUTO_ENABLED if allow_auto is None else allow_auto
        self.provider = ""
        self.step = 1
        self.api_models = None
        self.fetched = False

    def compose(self) -> ComposeResult:
        with Vertical(id="connection-form", classes="dialog"):
            yield Label("", id="connection-title", classes="dialog-title")
            with Vertical(id="step-provider"):
                yield Static("AI OS 负责调度整个写作流程；各专业节点用哪个模型，在顶栏“分工”里另设。", classes="dialog-note")
                with RadioSet(id="provider"):
                    for key, name in self.PROVIDERS:
                        if key != "auto" or self.allow_auto:
                            yield RadioButton(name, id=f"opt-{key}")
                yield Static("正在检测本机的 Codex 与 Claude Code……", id="detect-note", classes="dialog-note")
            with Vertical(id="step-model"):
                yield Input(placeholder="API 地址", id="base-url")
                yield Input(placeholder="API key（不显示、不落盘）", password=True, id="api-key")
                with Horizontal(id="fetch-row"):
                    yield Button("读取模型列表", id="fetch-models")
                    yield Static("", id="fetch-note")
                yield Label("", id="list-title", classes="list-title")
                yield OptionList(id="models")
                yield Input(id="model", placeholder="模型标识，可以随手写（如 opus 5.5），接入前会规范成标准写法")
                yield Label("Claude Code 模型（Codex 额度不足时改用）", id="list-title-claude", classes="list-title")
                yield OptionList(id="models-claude")
                yield Input(id="claude-model", placeholder="Claude Code 模型标识")
            # 提示与按钮固定在弹窗底部；内容比终端高时只滚动上面的列表，按钮始终看得到。
            with Vertical(id="connection-footer"):
                yield Static("", id="connection-note")
                with Horizontal(classes="dialog-buttons"):
                    if self.initial:
                        yield Button("退出", id="quit-app")
                    else:
                        yield Button("取消", id="cancel-connection")
                    yield Button("上一步", id="prev-step")
                    yield Button("下一步", id="next-step", variant="primary")
                    yield Button("接入", id="apply-connection", variant="primary")

    def on_mount(self):
        for key in ("auto", "codex", "claude"):
            for button in self.query(f"#opt-{key}"):
                button.disabled = True
        self.show_step(1)
        if self.clis is not None:
            self.show_detection(self.clis)
        else:
            self.detect()

    # ---------- 第 1 步：接入方式 ----------

    @work(thread=True, group="cli-detect")
    def detect(self):
        from ai_os_setup import detect_clis
        clis = (self.detector or detect_clis)()
        try:
            self.app.call_from_thread(self.show_detection, clis)
        except RuntimeError:
            return  # 页面已关闭

    def show_detection(self, clis):
        from textual.css.query import NoMatches
        from agent_cli import display_name
        self.clis = clis
        self.app.clis = clis
        try:
            found = [info for info in clis.values() if info.available]
            for key in ("codex", "claude"):
                info = clis[key]
                button = self.query_one(f"#opt-{key}", RadioButton)
                name = dict(self.PROVIDERS)[key]
                button.label = (f"{name}  ✓ {info.version} · {info.reason}" if info.available
                                else f"{name}  ✗ {info.reason}")
                button.disabled = not info.available
            for auto in self.query("#opt-auto"):
                auto.label = "自动  按剩余额度依次用 Codex → Claude Code → DeepSeek API"
                auto.disabled = not found
            self.query_one("#detect-note", Static).update(
                ("已检测到 " + "、".join(display_name(i.provider) for i in found) + "，可直接用本机登录接入。")
                if found else "本机没有检测到 Codex 或 Claude Code，请用 API 接入。")
            enabled = [key for key, _ in self.PROVIDERS if any(not b.disabled for b in self.query(f"#opt-{key}"))]
            remembered = self.remembered.get("provider")
            choice = remembered if remembered in enabled else next(
                (key for key in ("auto", "codex", "claude") if key in enabled), "deepseek")
            self.query_one(f"#opt-{choice}", RadioButton).value = True
        except NoMatches:
            return

    def chosen_provider(self):
        pressed = self.query_one("#provider", RadioSet).pressed_button
        return str(pressed.id).removeprefix("opt-") if pressed is not None else ""

    def show_step(self, step):
        self.step = step
        self.query_one("#step-provider").display = step == 1
        self.query_one("#step-model").display = step == 2
        self.query_one("#prev-step").display = step == 2
        self.query_one("#next-step").display = step == 1
        self.query_one("#apply-connection").display = step == 2
        self.query_one("#connection-note", Static).update("")
        title = "选择 AI OS 模型 · 第 1 步：接入方式" if step == 1 else \
            f"选择 AI OS 模型 · 第 2 步：{dict(self.PROVIDERS).get(self.provider, '')} 的模型"
        self.query_one("#connection-title", Label).update(title)

    @on(Button.Pressed, "#next-step")
    def next_step(self):
        provider = self.chosen_provider()
        if not provider:
            self.query_one("#connection-note", Static).update("请先选择一种接入方式。")
            return
        if provider in {"auto", "codex", "claude"} and self.clis is None:
            self.query_one("#connection-note", Static).update("正在检测本机 CLI，请稍候。")
            return
        self.provider = provider
        self.show_step(2)
        self.build_model_step()

    @on(Button.Pressed, "#prev-step")
    def prev_step(self):
        self.show_step(1)

    # ---------- 第 2 步：模型 ----------

    def _fill(self, selector, options, remembered):
        """填入候选模型并预选上次的选择；上次是手写的模型名时预选“手动输入”并回填。"""
        listing = self.query_one(selector, OptionList)
        listing.clear_options()
        listing.add_options([Option(label, id=key) for key, label in options])
        keys = [key for key, _ in options]
        if remembered and remembered in keys:
            listing.highlighted = keys.index(remembered)
        elif remembered:
            listing.highlighted = keys.index(self.MANUAL)
        else:
            listing.highlighted = 0
        return keys[listing.highlighted] if keys else ""

    def build_model_step(self):
        import config
        from ai_os_setup import claude_catalog
        provider, clis = self.provider, self.clis or {}
        codex, claude = clis.get("codex"), clis.get("claude")
        same = self.remembered.get("provider") == provider
        manual = (self.MANUAL, "其他模型（手动输入）")
        self.api_models, self.fetched = None, False
        is_api = provider in {"deepseek", "api", "anthropic"}
        # 自动模式要同时列 Codex 与 Claude 两张表，各自压矮，保证常见终端高度下一屏放得下。
        self.query_one("#connection-form").set_class(provider == "auto", "-auto")
        self.query_one("#base-url").display = provider in {"api", "anthropic"}
        self.query_one("#api-key").display = is_api
        self.query_one("#fetch-row").display = is_api
        self.query_one("#fetch-note", Static).update("")
        base_url, key = self.query_one("#base-url", Input), self.query_one("#api-key", Input)
        base_url.value = self.remembered.get("base_url", "") if same else ""
        base_url.placeholder = ("Anthropic 兼容地址，如 https://api.example.com/anthropic" if provider == "anthropic"
                                else "OpenAI 兼容地址，如 https://api.example.com/v1")
        key.placeholder = ("API key（已从 .env 读取 DeepSeek 密钥，可留空）"
                           if provider == "deepseek" and config.PROVIDERS["deepseek"].get("api_key")
                           else "API key（不显示、不落盘）")
        remembered = self.remembered.get("model", "") if same else ""
        title = self.query_one("#list-title", Label)
        if provider in {"auto", "codex"}:
            default = codex.default_model if codex else ""
            options = [(self.DEFAULT, "账户默认模型" + (f"（{default}）" if default else ""))]
            options += [(m, f"{m}  · {name}" if name.lower() != m.lower() else m) for m, name, _ in (codex.models if codex else ())]
            title.update("Codex 模型" + ("" if codex and codex.models else "（读不到账户模型列表，可手动输入）"))
        elif provider == "claude":
            options = [(self.DEFAULT, "CLI 默认模型")] + [(m, m) for m in claude_catalog()]
            title.update("Claude Code 模型")
        elif provider == "deepseek":
            options = [(config.ROLE_MODELS["architect"][1], config.ROLE_MODELS["architect"][1] + "（默认）")]
            title.update("DeepSeek 模型")
        else:
            options = []
            title.update("模型（填好地址和密钥后点“读取模型列表”，也可以直接手动输入）")
        chosen = self._fill("#models", options + [manual], remembered)
        self.query_one("#model", Input).value = remembered if chosen == self.MANUAL else ""
        self.query_one("#model").display = chosen == self.MANUAL
        in_auto = provider == "auto" and bool(claude and claude.available)
        for selector in ("#list-title-claude", "#models-claude"):
            self.query_one(selector).display = in_auto
        self.query_one("#models").display = provider != "auto" or bool(codex and codex.available)
        title.display = self.query_one("#models").display
        second = self.remembered.get("claude_model", "") if same else ""
        picked = self._fill("#models-claude", [(self.DEFAULT, "CLI 默认模型")] + [(m, m) for m in claude_catalog()]
                            + [manual], second) if in_auto else ""
        self.query_one("#claude-model", Input).value = second if picked == self.MANUAL else ""
        self.query_one("#claude-model").display = in_auto and picked == self.MANUAL
        if provider == "deepseek" and config.PROVIDERS["deepseek"].get("api_key"):
            self.fetch()  # 已有密钥就直接读取可选模型

    @on(OptionList.OptionHighlighted, "#models")
    def model_highlighted(self, event):
        self.query_one("#model").display = event.option.id == self.MANUAL
        if event.option.id == self.MANUAL:
            self.query_one("#model", Input).focus()

    @on(OptionList.OptionHighlighted, "#models-claude")
    def claude_highlighted(self, event):
        self.query_one("#claude-model").display = event.option.id == self.MANUAL

    @on(Button.Pressed, "#fetch-models")
    def fetch(self):
        self.query_one("#fetch-note", Static).update("正在读取……")
        self.query_one("#fetch-models", Button).disabled = True
        self.load_models(self.provider, self.query_one("#base-url", Input).value, self.query_one("#api-key", Input).value)

    @work(thread=True, group="model-list", exclusive=True)
    def load_models(self, provider, base_url, api_key):
        from ai_os_setup import fetch_models
        try:
            models, error = fetch_models(provider, base_url, api_key), ""
        except (ValueError, RuntimeError) as exc:
            models, error = None, str(exc)
        try:
            self.app.call_from_thread(self.models_loaded, provider, models, error)
        except RuntimeError:
            return

    def models_loaded(self, provider, models, error):
        from textual.css.query import NoMatches
        if provider != self.provider or self.step != 2:
            return
        try:
            self.query_one("#fetch-models", Button).disabled = False
            self.api_models, self.fetched = models, not error
            note = self.query_one("#fetch-note", Static)
            if error:
                note.update(error)
                return
            if not models:
                note.update("读不到模型列表（该服务可能不开放此接口），请手动输入模型。")
                return
            note.update(f"已读取 {len(models)} 个模型")
            current = self._selected_model("#models", "#model")
            chosen = self._fill("#models", [(m, m) for m in models] + [(self.MANUAL, "其他模型（手动输入）")],
                                current or self.remembered.get("model", ""))
            self.query_one("#model").display = chosen == self.MANUAL
        except NoMatches:
            return

    def _selected_model(self, list_selector, input_selector):
        listing = self.query_one(list_selector, OptionList)
        if listing.highlighted is None:
            return ""
        key = listing.get_option_at_index(listing.highlighted).id
        if key == self.DEFAULT:
            return ""
        return self.query_one(input_selector, Input).value if key == self.MANUAL else key

    @on(Button.Pressed, "#apply-connection")
    def apply(self):
        choice = {"provider": self.provider,
                  "model": self._selected_model("#models", "#model"),
                  "claude_model": self._selected_model("#models-claude", "#claude-model") if self.provider == "auto" else "",
                  "base_url": self.query_one("#base-url", Input).value,
                  "api_key": self.query_one("#api-key", Input).value}
        if self.fetched:
            choice["models"] = self.api_models
        self.query_one("#apply-connection", Button).disabled = True
        self.query_one("#connection-note", Static).update("正在核对模型……")
        self.verify(choice, dict(self.clis or {}))

    @work(thread=True, group="connection-verify")
    def verify(self, choice, clis):
        from ai_os_setup import prepare_connection
        try:
            settings, notes = prepare_connection(choice, clis)
        except (ValueError, RuntimeError) as exc:
            self.app.call_from_thread(self.rejected, str(exc) or "配置无效，请核对地址、模型和密钥。")
            return
        self.app.call_from_thread(self.dismiss, {"settings": settings, "notes": notes})

    def rejected(self, message):
        self.query_one("#connection-note", Static).update("未接入：" + message)
        self.query_one("#apply-connection", Button).disabled = False

    @on(Button.Pressed, "#cancel-connection")
    def cancel(self):
        self.dismiss(None)

    @on(Button.Pressed, "#quit-app")
    async def quit_app(self):
        await self.app.action_quit()

    def action_close(self):
        if not self.initial:
            self.dismiss(None)


class KeysScreen(ModalScreen):
    """快捷键说明；只列本页面真实绑定的键。"""
    BINDINGS = [Binding("escape", "close", "关闭", show=False)]
    KEYS = (("Ctrl+S / Ctrl+Enter", "提交输入框内容（等同操作行的“开始写作 / 提交修改意见 / 退回修改”等）"),
            ("F1", "打开本说明"),
            ("Esc", "关闭弹窗"),
            ("Tab / Shift+Tab", "在输入框和按钮之间切换"),
            ("Enter", "按下当前选中的按钮"),
            ("Ctrl+Q", "退出（任何页面都可用）；已完成节点的断点会保留"),
            ("Ctrl+C", "在接入页退出；其他页面的输入框内是复制"),
            ("Ctrl+C / Ctrl+X / Ctrl+V", "输入框内复制 / 剪切 / 粘贴"),
            ("Ctrl+Z / Ctrl+Y", "输入框内撤销 / 重做"),
            ("F7", "输入框内全选"),
            ("鼠标点击正文", "查看原文（可选中复制），移开鼠标恢复渲染"))

    def compose(self) -> ComposeResult:
        from rich.table import Table
        table = Table(box=None, show_header=False, padding=(0, 2, 0, 0))
        table.add_column(style="bold", no_wrap=True)
        table.add_column()
        for key, meaning in self.KEYS:
            table.add_row(key, meaning)
        with Vertical(id="keys-form", classes="dialog"):
            yield Label("快捷键", classes="dialog-title")
            yield Static(table)
            with Horizontal(classes="dialog-buttons"):
                yield Button("知道了", id="close-keys", variant="primary")

    @on(Button.Pressed, "#close-keys")
    def action_close(self):
        self.dismiss(None)


class SampleToggle(Checkbox):
    """勾选框：未选是空框 ☐，选中是 ☑，点一下切换。Textual 默认样式未选时也显示 X，容易误读成已选。"""
    @property
    def _button(self):
        from rich.text import Text
        return Text("☑" if self.value else "☐", style="bold" if self.value else "")


class RoleScreen(ModalScreen):
    """专业节点分工：每个角色可指定提供方/模型或沿用配置分工。下一节点边界生效，在途调用不变。

    本机没检测到的 CLI 不出现在下拉里（当前覆盖正在用的除外）；手写的模型名按接入页同一规则规范。
    """
    BINDINGS = [Binding("escape", "cancel", "关闭", show=False)]
    ROLE_NAMES = {"architect": "定框架", "researcher": "研究", "writer": "初稿", "reviewer": "内容审核",
                  "stylist": "润色", "final_check": "成稿核验"}
    PROVIDER_OPTIONS = [("沿用默认", ""), ("Claude Code", "claude"), ("Codex", "codex"), ("CodeBuddy", "codebuddy"),
                        ("DeepSeek API", "deepseek"), ("千问 API", "dashscope")]

    def __init__(self, snapshot, clis=None):
        super().__init__()
        self.snapshot = dict(snapshot)
        self.clis = clis or {}

    def provider_options(self, current):
        missing = {key for key, info in self.clis.items() if not info.available}
        return [(label, key) for label, key in self.PROVIDER_OPTIONS if key not in missing or key == current]

    def compose(self) -> ComposeResult:
        from agent_cli import display_name
        state = self.snapshot
        with Vertical(id="role-form", classes="dialog"):
            yield Label("专业节点分工 · " + (state.get("topic") or "当前任务"), classes="dialog-title")
            yield Static("每个写作环节用哪个模型。“沿用默认”即右侧灰字所示；AI OS 自己用哪个模型在顶栏“接入”里设。",
                         classes="dialog-note")
            for role in ROLES[1:]:
                provider, model = state["config"].get(role, ("", ""))
                override = state["overrides"].get(role, {})
                with Horizontal(classes="role-row"):
                    yield Static(f"[b]{self.ROLE_NAMES.get(role, role)}[/b] [dim]{role}[/dim]\n"
                                 f"[dim]默认 {display_name(provider)}" + (f" / {model}" if model else "") + "[/dim]",
                                 classes="role-name")
                    yield Select(self.provider_options(override.get("provider", "")), value=override.get("provider", ""),
                                 allow_blank=False, id=f"role-provider-{role}", classes="role-provider")
                    yield Input(value=override.get("model", ""), placeholder="模型（留空用该接入的默认模型）",
                                id=f"role-model-{role}", classes="role-model")
            recent = state.get("routes") or []
            yield Static("最近实际接入：" + ("；".join(
                f"{r.get('role')}→{display_name(r.get('provider', ''))}" + (f"/{r.get('model')}" if r.get('model') else "")
                for r in recent) if recent else "尚无") + "\n设置在下一节点边界生效；正在执行的调用沿用其开始时的分工。",
                id="role-note", classes="dialog-note")
            yield Static("", id="role-error")
            with Horizontal(classes="dialog-buttons"):
                yield Button("取消", id="cancel-roles")
                yield Button("应用分工", id="apply-roles", variant="primary")

    @on(Button.Pressed, "#apply-roles")
    def apply(self):
        from ai_os_setup import claude_catalog, normalize_model
        from agent_cli import display_name
        settings = {}
        try:
            for role in ROLES[1:]:
                provider = str(self.query_one(f"#role-provider-{role}", Select).value or "")
                raw = self.query_one(f"#role-model-{role}", Input).value
                if not provider:
                    settings[role] = None
                    continue
                codex = self.clis.get("codex")
                catalog = (list(claude_catalog()) if provider == "claude"
                           else [m for m, _, _ in codex.models] if provider == "codex" and codex else [])
                try:
                    model, _ = normalize_model(provider, raw, catalog, authoritative=provider == "codex" and bool(catalog))
                except RuntimeError as exc:
                    raise ValueError(f"{self.ROLE_NAMES.get(role, role)}（{display_name(provider)}）：{exc}") from None
                settings[role] = {"provider": provider, "model": model}
        except ValueError as exc:
            self.query_one("#role-error", Static).update("未应用：" + str(exc))
            return
        self.dismiss({"task_id": self.snapshot["task_id"], "settings": settings})

    @on(Button.Pressed, "#cancel-roles")
    def action_cancel(self):
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


class HistoryScreen(ModalScreen):
    """历史：按来源分组列出任务与已发布文章；只切换页面所看的内容，不触发任何续跑或确认。"""
    BINDINGS = [Binding("escape", "cancel", "关闭", show=False)]

    def __init__(self, groups):
        super().__init__()
        self.groups = [(title, list(items)) for title, items in groups if items]

    def compose(self) -> ComposeResult:
        from textual.widgets.option_list import Separator
        with Vertical(id="history-form", classes="dialog"):
            yield Label("历史", classes="dialog-title")
            if self.groups:
                options = []
                for title, items in self.groups:
                    if options:
                        options.append(Separator())
                    options.append(Option(f"[b]{title}[/b]", disabled=True))
                    options += [Option("  " + label, id=key) for label, key in items]
                yield OptionList(*options, id="history-list")
            else:
                yield Static("还没有历史任务。", classes="dialog-note")
            with Horizontal(classes="dialog-buttons"):
                yield Button("取消", id="cancel-history")

    @on(OptionList.OptionSelected, "#history-list")
    def chosen(self, event):
        self.dismiss(event.option.id)

    @on(Button.Pressed, "#cancel-history")
    def action_cancel(self):
        self.dismiss(None)


class PreferenceScreen(ModalScreen):
    """本篇偏好单独填写，不与修改意见共用输入框，避免一段话被同时当成意见和偏好。"""
    def compose(self) -> ComposeResult:
        with Vertical(id="preference-form", classes="dialog"):
            yield Label("记录本篇偏好", classes="dialog-title")
            yield Static("只作用于当前这篇文章，供后续节点参考；不构成对任何内容的确认。", classes="dialog-note")
            yield TextArea(id="preference-text")
            with Horizontal(classes="dialog-buttons"):
                yield Button("记录", id="apply-preference", variant="primary")
                yield Button("取消", id="cancel-preference")

    @on(Button.Pressed, "#apply-preference")
    def apply(self):
        text = self.query_one("#preference-text", TextArea).text.strip()
        if text:
            self.dismiss(text)

    @on(Button.Pressed, "#cancel-preference")
    def cancel(self):
        self.dismiss(None)


class Composer(TextArea):
    """带占位提示的输入框：空且未获得焦点时显示提示，点进去即消失。Textual 1.0 的 TextArea 没有 placeholder。"""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._hint = ""

    def set_hint(self, hint):
        if hint != self._hint:
            self._hint = hint
            self.refresh()

    def on_focus(self):
        self.refresh()

    def on_blur(self):
        self.refresh()

    def render_line(self, y):
        if self.text or self.has_focus or not self._hint:
            return super().render_line(y)
        from rich.segment import Segment
        from rich.style import Style
        from textual.strip import Strip
        base = self.rich_style
        width = self.scrollable_content_region.width
        if y != 0:
            return Strip.blank(width, base)
        return Strip([Segment(" " + self._hint, base + Style(dim=True, italic=True))]).extend_cell_length(width, base)


class ChatView(VerticalScroll):
    """渲染视图：点击正文切到原文。滚动条上的点击只是滚动，不切换。"""
    def on_click(self, event):
        from textual.scrollbar import ScrollBar, ScrollBarCorner
        if not isinstance(event.widget, (ScrollBar, ScrollBarCorner)):
            self.app.show_raw()


class RawView(TextArea):
    """原文视图（只读，可选中复制）：鼠标移出或失去焦点后回到渲染视图。"""
    def on_leave(self):
        # 移到自身滚动条上也会收到 Leave；稍后按区域再核一次，指针仍在框内就不切。
        self.set_timer(0.2, self._leave_if_outside)

    def _leave_if_outside(self):
        if not self.is_mouse_over:
            self.app.show_rendered()

    def on_blur(self):
        self.app.show_rendered()


STEPS = ("材料", "摘要确认", "写作与审核", "人工终审", "保存", "发布")

# 阶段 → (状态名, 操作行提示, 输入框占位提示)。页面只显示当前阶段用得上的输入与按钮。
STAGES = {
    "idle": ("未开始", "填写主题和观点材料。AI OS 先生成摘要，经你确认后才动笔。", "在此输入观点与材料……"),
    "running": ("运行中", "AI OS 工作中；补充说明会在下一节点边界读取。", "在此补充说明……"),
    "summary": ("待确认摘要", "摘要准确就确认；有出入就写修改意见（提交意见不等于通过）。", "在此输入修改意见……"),
    "sample": ("待选短样稿", "点选一版，或写修改意见。", "在此输入修改意见……"),
    "decision": ("待你决定", "AI OS 需要你的回复。", "在此输入你的回复……"),
    "final": ("待终审", "确认署名后保存到本地；要改就写意见再退回。发布另行确认。", "在此输入修改意见……"),
    "completed": ("已保存", "已保存到本地。发布前先看预览再单独确认；要改就写意见点“退回修改”。",
                  "写下修改意见，点“退回修改”会基于这一版开一轮修订……"),
    "failed": ("已中断", "断点已保留：直接续跑；额度不够就先追加预算。", ""),
    "archive": ("已发布（只读）", "要改就写意见点“退回修改”，会基于这一版开一轮修订；写新文章点“＋ 新文章”。",
                "写下修改意见，点“退回修改”会基于这一版开一轮修订……"),
}
# 操作行按钮按固定顺序排列（主操作在最右），每个阶段只显示其中几个。
STAGE_BUTTONS = {
    "idle": {"sample", "send"},
    "running": {"send"},
    "summary": {"send", "approve"},
    "sample": {"send", "choose-a", "choose-b"},
    "decision": {"send"},
    "final": {"send", "approve"},
    "completed": {"publish-preview", "send"},
    "failed": {"budget", "retry"},
    "archive": {"send"},
}
SEND_LABELS = {"idle": "开始写作", "running": "补充说明", "summary": "提交修改意见", "sample": "提交修改意见",
               "decision": "回复", "final": "退回修改", "completed": "退回修改", "archive": "退回修改"}
ACTION_IDS = ("sample", "budget", "send", "choose-a", "choose-b", "retry", "approve", "publish-preview", "publish-confirm")


def short_connection(settings) -> str:
    """状态行用的短写法；完整描述（含“调用时按额度决定”）写在对话区。"""
    from ai_os_connection import describe_settings
    if settings.provider != "auto":
        return describe_settings(settings)
    return "自动（" + " → ".join([settings.model or "Codex 默认", settings.claude_model or "Claude Code 默认", "DeepSeek"]) + "）"


def stage_of(status) -> str:
    if not status or not status.get("task_id"):
        return "idle"
    state = status.get("status")
    if state in {"pending", "running"}:
        return "running"
    if state == "awaiting_human":
        kind = (status.get("interrupt") or {}).get("kind")
        return kind if kind in {"summary", "sample", "final"} else "decision"
    return "completed" if state == "completed" else "failed"


def step_of(stage, status, published=False) -> int:
    """当前所处的步骤序号；等于 len(STEPS) 表示全部完成。"""
    fixed = {"idle": 0, "summary": 1, "sample": 2, "decision": 2, "final": 3, "archive": len(STEPS)}
    if stage in fixed:
        return fixed[stage]
    if stage == "completed":
        return len(STEPS) if published else 5
    if status.get("save_started"):
        return 4
    # 运行中/中断时按时间线判断是否已越过摘要环节。
    beyond = any(item.get("node") not in {"summary", "human_summary"} for item in status.get("timeline") or [])
    return 2 if beyond else 1


def steps_text(current: int):
    from rich.text import Text
    text = Text()
    for index, name in enumerate(STEPS):
        if index:
            text.append("  ─  ", style="dim")
        if index < current:
            text.append("✓ " + name, style="green")
        elif index == current:
            text.append(" ● " + name + " ", style="bold reverse")
        else:
            text.append("○ " + name, style="dim")
    return text


class WritingApp(App):
    TITLE = "写作管道 · AI OS"
    ENABLE_COMMAND_PALETTE = False
    CSS = """
    Screen { background: $background; }
    Button { border: none; height: 1; min-width: 6; padding: 0 2; margin-left: 1; text-style: none; }
    Button:hover { background: $boost; }
    Button.-primary { background: $primary; color: $text; text-style: bold; }
    Button.-primary:hover { background: $primary-lighten-1; }
    Button.-success { background: $success; color: $background; text-style: bold; }
    Button.-success:hover { background: $success-lighten-1; }
    Button.-warning { background: $warning; color: $background; text-style: bold; }
    Button.-warning:hover { background: $warning-lighten-1; }
    Button.-active { tint: $background 20%; }
    Button:disabled { text-opacity: 0.5; }

    #topbar { height: 1; background: $panel; padding: 0 1; }
    #brand { width: auto; color: $accent; text-style: bold; padding-right: 2; }
    #task-title { width: 1fr; color: $text-muted; }
    #topbar Button { background: $panel; color: $text-muted; margin-left: 0; padding: 0 1; }
    #topbar Button:hover { background: $boost; color: $text; }
    #topbar #new { color: $accent; text-style: bold; }

    #progress { height: auto; padding: 0 2; margin-top: 1; }
    #steps { height: 1; }
    #status { height: auto; max-height: 2; color: $text-muted; }

    #chat-panel { height: 1fr; margin: 1 1 0 1; border: round $primary 50%; padding: 0 1; }
    #chat-panel:focus-within { border: round $primary; }
    #chat-head { height: 1; }
    #chat-label { width: 1fr; color: $text-muted; }
    #render-toggle { background: transparent; color: $text-muted; margin: 0; padding: 0 1; }
    #render-toggle:hover { background: $boost; color: $text; }
    #chat-view { height: 1fr; scrollbar-size-vertical: 1; }
    #chat-raw { height: 1fr; border: none; padding: 0; background: $surface; display: none; }
    .msg { height: auto; margin: 0 0 1 0; padding: 0 1; }
    .msg-title { color: $success; text-style: bold; }
    .msg-ai { border-left: outer $success; }
    .msg-ai Markdown { padding: 0; margin: 0; background: transparent; }
    .msg-user { border-left: outer $accent; background: $boost; }
    .msg-note { color: $text-muted; }

    #composer { height: auto; margin: 1 1 0 1; background: $background; }
    #topic { border: round $primary 50%; background: $background; }
    #topic:focus { border: round $accent; }
    #message { height: 6; border: round $primary 50%; background: $background; }
    #message:focus { border: round $accent; }

    #actions { height: 1; margin: 1 1; }
    #hint { width: 1fr; color: $text-muted; padding-left: 1; }
    #sample { border: none; height: 1; padding: 0 1; background: transparent; }
    #sample:focus { background: transparent; }
    #sample:focus > .toggle--label { background: transparent; text-style: underline; }

    .dialog { width: 80; height: auto; max-height: 90%; padding: 1 2; background: $surface; border: round $primary; }
    .dialog-title { text-style: bold; color: $accent; margin-bottom: 1; }
    .dialog-note { color: $text-muted; height: auto; margin: 1 0; }
    .dialog-buttons, #budget-form Horizontal, #fetch-row { height: 1; margin-top: 1; align-horizontal: right; }
    ConnectionScreen, BudgetScreen, RoleScreen, HistoryScreen, PreferenceScreen, KeysScreen { align: center middle; }
    #connection-form { width: 96; max-height: 95%; overflow-y: auto; }
    #connection-footer { dock: bottom; height: auto; background: $surface; }
    #connection-form.-auto #models, #connection-form.-auto #models-claude { max-height: 8; }
    #connection-form Input { margin: 1 0 0 0; border: round $primary 50%; background: $surface; }
    #connection-form Input:focus { border: round $accent; }
    #step-provider, #step-model { height: auto; }
    #provider { border: none; background: transparent; width: 100%; padding: 0; }
    #provider:focus-within { border: none; }
    #provider RadioButton { background: transparent; width: 100%; }
    #detect-note { margin: 1 0 0 0; }
    #fetch-row { height: 1; margin-top: 1; }
    #quit-app { dock: left; margin-left: 0; }
    #fetch-row Button { margin-left: 0; background: $boost; }
    #fetch-note { width: 1fr; color: $text-muted; padding-left: 1; }
    .list-title { color: $text-muted; margin-top: 1; }
    #models, #models-claude { height: auto; max-height: 14; border: round $primary 50%; background: $surface; }
    #models:focus, #models-claude:focus { border: round $accent; }
    #connection-note { color: $warning; height: auto; }
    #keys-form { width: 90; }
    #budget-form { width: 80; height: auto; padding: 1 2; background: $surface; border: round $warning; }
    #budget-form Input { margin: 1 0 0 0; }
    #budget-note { height: auto; color: $text-muted; margin-top: 1; }
    #role-form { width: 112; max-height: 95%; overflow-y: auto; }
    #role-form > .dialog-buttons { dock: bottom; background: $surface; }
    .role-row { height: 3; }
    .role-name { width: 36; height: 3; padding-top: 0; }
    .role-provider { width: 26; }
    .role-provider > SelectCurrent { border: round $primary 50%; background: $surface; }
    .role-provider:focus > SelectCurrent { border: round $accent; }
    .role-provider > SelectCurrent .arrow { color: $accent; }
    .role-model { width: 1fr; margin-left: 1; border: round $primary 50%; background: $surface; }
    .role-model:focus { border: round $accent; }
    #role-error { color: $warning; height: auto; }
    #role-note { height: auto; color: $text-muted; margin-top: 1; }
    #history-list { height: auto; max-height: 20; }
    #preference-text { height: 6; }
    """
    # 页面全部走按钮，不显示页脚快捷键；顶栏“快捷键”（或 F1）列出全部可用键。
    BINDINGS = [Binding("ctrl+enter,ctrl+s", "send", "发送", show=False), Binding("f1", "keys", "快捷键", show=False),
                Binding("ctrl+q", "quit", "退出并保留断点", show=False, priority=True)]

    def __init__(self, directory: Path, *, controller=None, choose_connection=False, detector=None, mock=False):
        """choose_connection=True 时先显示接入页（正式启动）；测试与嵌入场景默认跳过，沿用自动接入。"""
        super().__init__()
        from ai_os_setup import load_choice
        self.choose_connection = choose_connection
        self.mock = mock  # 模拟模式：占位稿不能发布，页面标明“模拟”
        self.detector = detector
        self.clis = None
        self.remembered = load_choice(directory)
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
        self.route_note = "AI OS：" + short_connection(self.settings)
        self._routes_shown = 0
        self.transcript = []
        self.auto_render = True
        self._raw_stale = True
        self._stage_key = None
        self._published = False
        self._connection_chosen = False
        self.home = directory  # 启动时的任务目录：接入选择与历史来源登记都记在这里
        self._viewing_article = None
        self._history_dirs, self._history_articles = [], []

    def compose(self) -> ComposeResult:
        with Horizontal(id="topbar"):
            yield Static("✎ 写作管道" + (" · 模拟" if self.mock else ""), id="brand")
            yield Static("新文章", id="task-title")
            yield Button("历史", id="history")
            yield Button("接入", id="connection")
            yield Button("分工", id="roles")
            yield Button("偏好", id="preference")
            yield Button("快捷键", id="keys")
            yield Button("＋ 新文章", id="new")
        with Vertical(id="progress"):
            yield Static(id="steps")
            yield Static(self.route_note, id="status")
        with Vertical(id="chat-panel"):
            with Horizontal(id="chat-head"):
                yield Static("对话", id="chat-label")
                yield Button("渲染：开", id="render-toggle")
            yield ChatView(id="chat-view")
            yield RawView(id="chat-raw", read_only=True, soft_wrap=True)
        with Vertical(id="composer"):
            yield Input(placeholder="文章主题", id="topic")
            yield Composer(id="message")
        with Horizontal(id="actions"):
            yield Static(id="hint")
            yield SampleToggle("先确认短样稿", id="sample")
            yield Button("追加预算", id="budget")
            yield Button("开始写作", id="send")
            yield Button("选 A", id="choose-a")
            yield Button("选 B", id="choose-b")
            yield Button("续跑", id="retry", variant="primary")
            yield Button("确认", id="approve", variant="success")
            yield Button("发布预览", id="publish-preview")
            yield Button("确认发布", id="publish-confirm", variant="warning")

    def on_mount(self):
        from tools.storage import exclusive
        self._directory_guard = exclusive(self.directory / ".ui.lock")
        try:
            self._directory_guard.__enter__()
        except RuntimeError:
            self._directory_guard = None
            self.exit(message="这个TUI任务目录已在另一个终端打开；请使用原窗口或另选--directory。")
            return
        self.theme = "tokyo-night"
        if not self.controller.task_id:
            self.post("输入主题和观点材料开始。AI OS 先生成摘要等你确认；提交修改意见本身不表示通过。"
                      "退出（Ctrl+Q）会保留已完成节点的断点。")
        self.refresh_status()
        self.set_interval(0.5, self.refresh_status)
        if self.choose_connection:
            self.push_screen(ConnectionScreen(initial=True, remembered=self.remembered, detector=self.detector),
                             self.connection_changed)

    # ---------- 对话区 ----------

    def _main(self):
        # 模态表单压在屏幕栈顶部，主页面控件始终在栈底。
        return self.screen_stack[0]

    def chat_text(self) -> str:
        return "\n\n".join(item["raw"] for item in self.transcript)

    def post(self, text, kind="note", title="", markdown=None):
        """对话区追加一条：user=你说的，ai=待你确认的内容（按 Markdown 渲染），note=过程记录。"""
        from rich.text import Text
        text = str(text)
        raw = f"{title}\n{text}" if title else text
        if kind == "user":
            raw = "你：" + text
        self.transcript.append({"raw": raw})
        self._raw_stale = True
        view = self._main().query_one("#chat-view", ChatView)
        if kind == "ai":
            card = Vertical(Static(Text(title or "AI OS"), classes="msg-title"), Markdown(markdown or text),
                            classes="msg msg-ai")
        elif kind == "user":
            card = Static(Text(text), classes="msg msg-user")
        else:
            card = Static(Text(text), classes="msg msg-note")
        view.mount(card)
        view.call_after_refresh(view.scroll_end, animate=False)
        raw_view = self._main().query_one("#chat-raw", RawView)
        if raw_view.display:
            self._sync_raw(raw_view)

    def clear_chat(self):
        self.transcript.clear()
        self._raw_stale = True
        self._main().query_one("#chat-view", ChatView).remove_children()

    def _sync_raw(self, raw_view):
        raw_view.load_text(self.chat_text())
        raw_view.move_cursor(raw_view.document.end)
        self._raw_stale = False

    def show_raw(self):
        main = self._main()
        view, raw_view = main.query_one("#chat-view", ChatView), main.query_one("#chat-raw", RawView)
        if raw_view.display:
            return
        ratio = view.scroll_y / view.max_scroll_y if view.max_scroll_y else 1.0
        view.display, raw_view.display = False, True
        if self._raw_stale:
            self._sync_raw(raw_view)
        raw_view.focus()
        main.query_one("#chat-label", Static).update("对话 · 原文（鼠标移开后恢复渲染）" if self.auto_render else "对话 · 原文")
        raw_view.call_after_refresh(lambda: raw_view.scroll_to(y=ratio * raw_view.max_scroll_y, animate=False))

    def show_rendered(self):
        if not self.auto_render:
            return
        main = self._main()
        main.query_one("#chat-raw", RawView).display = False
        main.query_one("#chat-view", ChatView).display = True
        main.query_one("#chat-label", Static).update("对话")

    def toggle_render(self):
        self.auto_render = not self.auto_render
        self._main().query_one("#render-toggle", Button).label = "渲染：开" if self.auto_render else "渲染：关"
        if self.auto_render:
            self.show_rendered()
        else:
            self.show_raw()

    # ---------- 阶段与按钮 ----------

    def apply_stage(self, status):
        """只显示当前阶段用得上的输入与按钮；确认类按钮另要求界面已展示待确认内容。"""
        main = self._main()
        stage = self.current_stage(status)
        payload = status.get("interrupt") or {}
        paused = stage == "decision" and bool(payload.get("pause_reason"))
        preview_ready = stage == "completed" and self.controller.publish_preview is not None
        key = (stage, paused, preview_ready, self._action_pending, self.controller.displayed is not None,
               status.get("task_id"), status.get("topic"), status.get("output_path"), self._published,
               step_of(stage, status, self._published))
        if key == self._stage_key:
            return
        self._stage_key = key
        name, hint, placeholder = STAGES[stage]
        visible = set(STAGE_BUTTONS[stage])
        if paused:
            visible.add("budget")
        if preview_ready:
            visible.add("publish-confirm")
        if self.mock:
            visible -= {"publish-preview", "publish-confirm"}
        for identity in ACTION_IDS:
            widget = main.query_one("#" + identity)
            widget.display = identity in visible
            # 隐藏的按钮同时禁用，快捷键或测试驱动都不能越过阶段。
            widget.disabled = identity not in visible
        main.query_one("#approve", Button).disabled = not ("approve" in visible and self.controller.displayed is not None)
        main.query_one("#approve", Button).label = "确认署名并保存本地" if stage == "final" else "确认摘要"
        main.query_one("#budget", Button).disabled = bool("budget" not in visible or self._action_pending
                                                          or status.get("pipeline_version") != "v2")
        send = main.query_one("#send", Button)
        send.label = SEND_LABELS.get(stage, "发送")
        send.variant = "primary" if stage in {"idle", "running", "sample", "decision", "archive"} else "default"
        main.query_one("#publish-preview", Button).variant = "default" if preview_ready else "primary"
        main.query_one("#composer").display = "send" in visible
        main.query_one("#topic").display = stage == "idle"
        main.query_one("#message", Composer).set_hint(placeholder)
        if paused:
            hint = f"AI OS 已暂停：{payload['pause_reason']}。可回复，或追加预算后从暂停点继续。"
        elif stage == "completed":
            hint = ("已发布。" if self._published else
                    "核对预览无误后再确认发布。" if preview_ready else hint) + "  " + status.get("output_path", "")
        if self._action_pending:
            hint = "正在提交……"
        main.query_one("#hint", Static).update(hint)
        main.query_one("#task-title", Static).update(
            status.get("topic") or (self._viewing_article or {}).get("title") or "新文章")
        has_task = bool(status)
        main.query_one("#roles").display = has_task
        main.query_one("#preference").display = has_task and status.get("pipeline_version") == "v2"
        main.query_one("#steps", Static).update(steps_text(step_of(stage, status, self._published)))
        if not status:
            main.query_one("#status", Static).update(self.route_note)

    # ---------- 生命周期 ----------

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

    def _show_routes(self, status):
        routes = status.get("routes") or []
        if self.marker is not None and self.marker[0] != status.get("task_id"):
            self._routes_shown = 0
        if len(routes) < self._routes_shown:
            self._routes_shown = 0
        fresh = routes[self._routes_shown:]
        if not fresh:
            return
        from agent_cli import display_name
        for route in fresh:
            role = route.get("role") or "节点"
            label = "AI OS" if role == "orchestrator" else role
            self.post(f"{label} 已接入 {display_name(route.get('provider', ''))}"
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
        if self._quitting or not self.screen_stack:
            return
        view = self._main()
        self._history_ids = tuple(key for _, key in self.controller.history_options())
        status = self.controller.status()
        if not status or not status.get("task_id"):
            self.apply_stage({})
            return
        self._show_routes(status)
        heartbeat = status.get("heartbeat") or {}
        line = f"{STAGES[stage_of(status)][0]} · {self.route_note}"
        if heartbeat:
            line += f" · {heartbeat.get('role', '')} 已运行 {heartbeat.get('elapsed_s', '?')} 秒"
            if heartbeat.get("progress_source") == "cli":
                line += f" · CLI事件 {heartbeat.get('events', 0)} 个 · 可见文本 {heartbeat.get('content_chars', 0)} 字"
            elif heartbeat.get("phase") == "tool_wait":
                line += " · 等待工具返回"
        view.query_one("#status", Static).update(line)
        payload = status.get("interrupt")
        marker = (status.get("task_id"), status.get("status"), json.dumps(payload, ensure_ascii=False), str(status.get("timeline")))
        if marker != self.marker:
            self.marker = marker
            self._show_change(status, payload)
        self.apply_stage(status)

    def _show_change(self, status, payload):
        if payload:
            self.controller.show_interrupt(payload)
            kind = payload.get("kind")
            if kind == "sample":
                text = "短样稿：请点选一版，或直接提出修改意见。\n\n" + "\n\n".join(
                    str(k) + "：\n" + str(v) for k, v in payload.get("options", {}).items())
                markdown = "短样稿：请点选一版，或直接提出修改意见。\n\n" + "\n\n".join(
                    f"#### 方案 {k}\n\n{v}" for k, v in payload.get("options", {}).items())
            elif kind == "final":
                # 稿首/稿尾的“润色说明/修改说明”注释是节点间的工作记录，保存时会被剥离，不属于稿件正文。
                body = re.sub(r"<!--.*?-->", "", payload.get("polished", ""), flags=re.S)
                body = re.sub(r"\n{3,}", "\n\n", body).strip()
                if payload.get("article_version") is not None:
                    intro = (f"第 {payload.get('article_version')} 版稿件（摘要 v{payload.get('summary_version', '?')}）"
                             "已通过原意、事实、阅读质量审核和成稿核验。确认署名后保存本地；发布另行确认。")
                elif payload.get("forced_pass"):
                    # 旧流程（v1）到审核轮次上限会强制放行；不能写成“已通过”。
                    intro = ("旧流程终稿：审核未通过，已到轮次上限被强制放行，发布前请先看审核意见。"
                             + (f"\n遗留问题：{'；'.join(map(str, payload['quality_issues']))}" if payload.get("quality_issues") else ""))
                else:
                    intro = "旧流程终稿：已通过审核与成稿核验。确认署名后保存本地；发布另行确认。"
                text, markdown = intro + "\n\n" + body, f"> {intro}\n\n{body}"
            else:
                text = payload.get("question") or payload.get("summary") or json.dumps(payload, ensure_ascii=False, indent=2)
                markdown = None
            self.post(text, "ai", "AI OS · 待你确认", markdown)
        elif status.get("status") == "failed":
            self.post("运行失败，断点已保留：" + status.get("error", ""))
        elif status.get("status") == "completed":
            self.post("已按你的终审确认保存本地：" + status.get("output_path", "") + "\n发布仍需单独预览和明确授权。")
        elif status.get("timeline"):
            self.post("已完成节点：" + status["timeline"][-1]["node"])

    # ---------- 动作 ----------

    def submit_action(self, action, *args):
        task_id, displayed = self.controller.action_snapshot()
        self.submit_bound_action(action, args, task_id, displayed)

    def submit_bound_action(self, action, args, task_id, displayed):
        if self.mock and action in {"preview", "publish"}:
            self.notify("模拟模式的稿件是占位文本，不能发布", severity="warning")
            return
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
        self.post("未执行：" + message)
        self.notify(message, severity="warning")

    def action_finished(self):
        self._action_pending = False
        self.refresh_status()

    def show_action_result(self, action, result):
        if action == "preview":
            plan = result["plan"]
            self.post("发布预览（仍未发布）：\n" + "\n".join(
                f"{key}：{plan[key]}" for key in ("repository", "remote_url", "branch", "destination", "article_sha256"))
                + "\n\n完整正文：\n" + result["article"], "ai", "发布预览（仍未发布）",
                "\n".join(f"- {key}：`{plan[key]}`" for key in
                          ("repository", "remote_url", "branch", "destination", "article_sha256"))
                + "\n\n---\n\n" + result["article"])
            self.controller.show_publish_preview(result)
            self._stage_key = None
            self.query_one("#publish-confirm", Button).disabled = False
        elif action == "publish":
            self.post(f"任务 {result['task_id']} 发布结果：{result.get('status')}；网站部署状态：{result.get('deployment_status', '未核验')}")
            self._published = True
            self._stage_key = None
            self.query_one("#publish-confirm", Button).disabled = True
        elif action == "preference":
            self.post("已记录为本篇用户偏好。")
        elif action == "revise":
            self._viewing_article = None
            self.marker, self._routes_shown, self._published, self._stage_key = None, 0, False, None
            self.post(f"已基于第 {result['version']} 版开始修订（任务 {result['task_id']}）。AI OS 先把修改要求整理进摘要，"
                      "请确认摘要后再动笔；改好的稿件照常审核、终审，确认保存后接到这条版本线的末尾。")
        elif action == "configure_roles":
            from agent_cli import display_name
            overrides = result.get("role_settings") or {}
            described = "；".join(f"{role}={display_name(v['provider'])}" + (f" / {v['model']}" if v.get("model") else "")
                                 for role, v in overrides.items()) or "全部沿用配置分工"
            self.post(f"任务 {result['task_id']} 的专业节点分工已更新：{described}。{result.get('applies', '')}。"
                      "实际接入以对话区“已接入”记录为准。")
        elif action == "extend_budget":
            line = f"任务 {result['task_id']} 已追加 {result['added_calls']} 次模型请求、{result['added_seconds']} 秒模型执行预算"
            if result.get("added_steps") or result.get("added_revisions"):
                line += f"、{result.get('added_steps', 0)} 步 AI OS 调度、{result.get('added_revisions', 0)} 次每问题修订"
            if result.get("resumed"):
                self.post(line + "。AI OS 已从暂停点继续；这不构成对摘要、短样稿或最终稿的确认。")
            else:
                self.post(line + "。尚未续跑；失败或暂停任务请点击“续跑”，待确认任务请继续回应所展示的内容。")

    def _invalidate_preview(self):
        self.controller.invalidate_publish_preview()
        self._stage_key = None
        confirm = self.query_one("#publish-confirm", Button)
        confirm.disabled, confirm.display = True, False

    @on(TextArea.Changed, "#message")
    def message_changed(self):
        self._invalidate_preview()

    @on(Input.Changed, "#topic")
    def topic_changed(self):
        self._invalidate_preview()

    def action_send(self):
        if self._action_pending:
            return
        status = self.controller.status()
        stage = self.current_stage(status)
        if stage not in SEND_LABELS:
            return
        message = self.query_one("#message", Composer)
        text = message.text.strip()
        if not text:
            self.notify("请先在输入框写内容", severity="warning")
            return
        if stage == "idle" and not self.query_one("#topic", Input).value.strip():
            self.notify("请先填写文章主题", severity="warning")
            return
        self.post(text, "user")
        message.clear()
        if stage in {"completed", "archive"}:
            # 已保存/已发布的版本退回修改：基于这一版开一个修订任务，不改动原版本。
            base = status.get("output_path") if stage == "completed" else self._viewing_article.get("path", "")
            self.submit_action("revise", base, text)
        elif not self.controller.task_id:
            self.submit_action("start", self.query_one("#topic", Input).value, text,
                               self.query_one("#sample", Checkbox).value)
        else:
            self.submit_action("send", text)

    def choose_sample(self, choice):
        self.post(f"选择方案 {choice}", "user")
        self.submit_action("send", choice)

    @on(Button.Pressed)
    def buttons(self, event):
        key = event.button.id
        if key == "send":
            self.action_send()
        elif key in {"choose-a", "choose-b"}:
            self.choose_sample(key[-1].upper())
        elif key == "approve":
            self.submit_action("approve")
        elif key == "retry":
            self.submit_action("retry")
        elif key == "render-toggle":
            self.toggle_render()
        elif key == "history":
            self.push_screen(HistoryScreen(self.history_groups()), self.history_chosen)
        elif key == "connection":
            current = {"provider": self.settings.provider, "model": self.settings.model,
                       "claude_model": self.settings.claude_model, "base_url": self.settings.base_url}
            self.push_screen(ConnectionScreen(remembered=current if self._connection_chosen else self.remembered,
                                              clis=self.clis, detector=self.detector), self.connection_changed)
        elif key == "keys":
            self.action_keys()
        elif key == "roles":
            try:
                self.push_screen(RoleScreen(self.controller.role_snapshot(), self.clis), self.roles_changed)
            except ValueError as exc:
                self.notify(str(exc), severity="warning")
        elif key == "preference":
            self.push_screen(PreferenceScreen(), self.preference_given)
        elif key == "budget":
            if self._action_pending:
                self.notify("上一项操作尚未完成，请稍候", severity="warning")
            else:
                try:
                    self.push_screen(BudgetScreen(self.controller.budget_snapshot()), self.budget_changed)
                except ValueError as exc:
                    self.notify(str(exc), severity="warning")
        elif key == "publish-preview":
            self.submit_action("preview")
        elif key == "publish-confirm":
            self.submit_action("publish")
        elif key == "new":
            if self._action_pending or self.controller.status().get("status") in {"pending", "running"}:
                self.notify("当前任务仍在运行", severity="warning")
            else:
                self.controller.clear()
                self._reset_view()
                self.query_one("#topic", Input).value = ""
                self.post("新文章：填写主题和观点材料后开始。")
                self.refresh_status()
                self.query_one("#topic", Input).focus()

    def _reset_view(self):
        self._viewing_article = None
        self.marker = None
        self._routes_shown = 0
        self._published = False
        self._stage_key = None
        self.clear_chat()

    def preference_given(self, text):
        if text:
            self.submit_action("preference", text)

    def roles_changed(self, decision):
        if decision is not None:
            self.submit_bound_action("configure_roles", (decision["settings"],), decision["task_id"], None)

    def budget_changed(self, decision):
        if decision is not None:
            # 不用当前选择重新取task_id；后台execute会拒绝表单打开后切换任务。
            self.submit_bound_action("extend_budget", (decision["calls"], decision["seconds"],
                                                       decision.get("steps", 0), decision.get("revisions", 0)),
                                     decision["task_id"], None)

    def action_keys(self):
        if not isinstance(self.screen, KeysScreen):
            self.push_screen(KeysScreen())

    def connection_changed(self, result):
        if not result:
            return
        from ai_os_connection import describe_settings
        from ai_os_setup import save_choice
        self.settings = result["settings"]
        self._connection_chosen = True
        try:
            save_choice(self.home, self.settings)
        except OSError:
            pass  # 只是下次预选用，写不进去不影响本次接入
        # 这里只是选择；真正接入哪一家以每次调用前的额度核验为准，届时对话区会写“已接入”。
        self.route_note = "AI OS：" + short_connection(self.settings)
        notes = list(result.get("notes") or [])
        if self.controller.task_id:
            # 接入设置随任务线程启动时带入：在途步骤不受影响，下一次提交/确认/续跑起才用新选择。
            notes.append("当前任务从你的下一次操作（提交意见、确认或续跑）起改用这个接入；"
                         "正在执行的步骤不受影响，已完成的节点不会重跑。")
        self.post("AI OS 选择：" + describe_settings(self.settings) + "".join("\n· " + note for note in notes))
        self._stage_key = None
        self.refresh_status()

    STATUS_NAMES = {"completed": "已完成", "awaiting_human": "待确认", "failed": "失败", "interrupted": "中断",
                    "running": "运行中", "pending": "排队中"}

    def history_groups(self):
        """本机任务 + 登记的其他任务目录 + 已发布文章（只读）。选项 id 编码来源，选中后再定位。"""
        from service.tui_controller import article_title, load_history_sources, read_task_list
        import config
        sources = load_history_sources(self.home)
        home = Path(self.home).resolve()
        self._history_dirs = [home] + [d for d in sources["task_dirs"] if d.resolve() != home]
        self._history_articles = sources["articles"]
        groups = []
        for index, directory in enumerate(self._history_dirs):
            try:
                name = directory.relative_to(config.BASE_DIR.resolve()).as_posix()
            except ValueError:
                name = str(directory)
            items = [(f"{topic[:34]} · {self.STATUS_NAMES.get(state, state)}", f"task|{index}|{key}")
                     for topic, state, key in read_task_list(directory)]
            groups.append(("本机任务" if index == 0 else f"任务目录 · {name}", items))
        groups.append(("已发布文章（只读）", [(article_title(item["path"]), f"article|{index}")
                                        for index, item in enumerate(self._history_articles)]))
        return groups

    def history_chosen(self, choice):
        if not choice:
            return
        kind, _, rest = str(choice).partition("|")
        if kind == "article":
            self.show_article(self._history_articles[int(rest)])
            return
        index, _, key = rest.partition("|")
        if not self.switch_directory(self._history_dirs[int(index)]):
            return
        self.controller.select(key)
        self._reset_view()
        self.refresh_status()
        status = self.controller.status()
        output = Path(status.get("output_path") or "")
        if status.get("status") == "completed" and output.is_file():
            # 从历史打开已完成的文章时，把保存下来的正文一并展示，免得只看到一行保存路径。
            self.post(re.sub(r"<!--.*?-->", "", output.read_text(encoding="utf-8"), flags=re.S).strip(),
                      "ai", "已保存的文章")

    def current_stage(self, status):
        return "archive" if self._viewing_article and not status else stage_of(status)

    def _busy(self):
        if self._action_pending or self.controller.status().get("status") in {"pending", "running"}:
            self.notify("当前任务仍在运行，先等它停在确认点或完成", severity="warning")
            return True
        return False

    def switch_directory(self, directory) -> bool:
        """切到另一个任务目录：先拿到新目录的界面锁，再放开旧目录；任务数据与检查点都留在原处。"""
        from pathlib import Path as _Path
        from tools.storage import exclusive
        from service.tui_controller import TuiController
        directory = _Path(directory)
        if directory.resolve() == _Path(self.directory).resolve():
            return True
        if self._busy():
            return False
        guard = exclusive(directory / ".ui.lock")
        try:
            guard.__enter__()
        except RuntimeError:
            self.notify("这个任务目录已在另一个终端打开", severity="warning")
            return False
        self.controller.manager.shutdown(0.0)
        if self._directory_guard is not None:
            self._directory_guard.__exit__(None, None, None)
        self._directory_guard, self.directory = guard, directory
        self.controller = TuiController(directory)
        self._history_ids = tuple(key for _, key in self.controller.history_options())
        return True

    def show_article(self, item):
        """只读展示没有任务记录的已发布文章：不绑定任何任务，没有确认、保存或发布按钮。"""
        from service.tui_controller import article_title
        if self._busy():
            return
        self.controller.clear()
        self._reset_view()
        title = article_title(item["path"])
        self._viewing_article = {"title": title, "path": str(item["path"])}
        body = re.sub(r"<!--.*?-->", "", item["path"].read_text(encoding="utf-8"), flags=re.S).strip()
        self.post(body, "ai", "已发布文章（只读） · " + title)
        if item.get("note") and item["note"].is_file():
            self.post(item["note"].read_text(encoding="utf-8").strip(), "ai", "发布状态")
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
    parser.add_argument("--directory", type=Path, default=None,
                        help="任务目录（含 tasks.json 与 checkpoints.sqlite）；默认 .runtime/tui（模拟模式 .runtime/tui-mock），"
                             "可指向验收运行目录或 .runtime/service")
    parser.add_argument("--role", action="append", default=[], metavar="角色=提供方[:模型]",
                        help="仅本次启动显式改某个专业节点的模型，如 reviewer=claude:claude-fable-5-1；可重复")
    args = parser.parse_args()
    runtime = Path(os.getenv("WRITING_RUNTIME_DIR") or Path(__file__).parent / ".runtime")
    directory = args.directory or runtime / ("tui-mock" if args.mock else "tui")
    if args.mock:
        # 模拟模型会在任务检查点上继续写占位稿；打开真实任务目录会把真实文章覆盖掉，所以只用独立目录。
        registry = directory / "tasks.json"
        if args.directory and registry.is_file() and registry.read_text(encoding="utf-8").strip() not in {"", "{}"}:
            raise SystemExit("模拟模式不能打开已有真实任务的目录；去掉 --directory 即使用独立的 .runtime/tui-mock")
        os.environ.update(MOCK_LLM="1", MEMORY_ENABLED="0")
    apply_role_overrides(args.role)
    WritingApp(directory, choose_connection=True, mock=args.mock).run()


if __name__ == "__main__":
    main()
