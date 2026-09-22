"""AI OS 的进程内接入配置和额度门槛；不修改专业节点模型或持久化密钥。

Codex 使用官方 app-server account/rateLimits/read，只查询，不创建线程。
Claude 使用官方 statusline rate_limits；没有新鲜遥测时拒绝接入。
来源：https://developers.openai.com/codex/app-server
https://code.claude.com/docs/en/statusline
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
import json
import math
import os
from pathlib import Path
import queue
import re
import subprocess
import threading
import time
from typing import Callable
from urllib.parse import urlsplit

import config
from agent_cli import AgentError, AgentReply, command_for, display_name


DEFAULT_CLAUDE_QUOTA_FILE = config.CLAUDE_QUOTA_FILE


class ConnectionError(RuntimeError):
    pass


@dataclass(frozen=True)
class ConnectionSettings:
    provider: str = "auto"
    api_key: str = field(default="", repr=False)
    base_url: str = ""
    model: str = ""

    def __post_init__(self):
        if self.provider not in {"auto", "codex", "claude", "deepseek", "api"}:
            raise ConnectionError("不支持的 AI OS 接入方式")
        if self.base_url:
            url = urlsplit(self.base_url)
            if (not url.hostname or url.username or url.password or url.query or url.fragment
                    or (url.scheme != "https" and not
                        (url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1", "::1"}))):
                raise ConnectionError("API 地址须为 HTTPS（本机允许 HTTP），不能包含凭据、查询参数或片段")


@dataclass(frozen=True)
class QuotaStatus:
    provider: str
    remaining_percent: float | None
    observed_at: float
    reason: str = ""
    expires_at: float | None = None

    def permits(self, *, now: float | None = None, max_age: float = 120) -> bool:
        now = time.time() if now is None else now
        minimum = {"codex": 15, "claude": 10}.get(self.provider)
        return bool(minimum is not None and _number(self.remaining_percent)
                    and 0 <= self.remaining_percent <= 100
                    and _number(self.observed_at) and 0 <= now - self.observed_at <= max_age
                    and (self.expires_at is None or (_number(self.expires_at) and self.expires_at > now))
                    and self.remaining_percent >= minimum)


@dataclass(frozen=True)
class ConnectionSelection:
    provider: str
    model: str
    base_url: str = ""
    api_key: str = field(default="", repr=False)
    reason: str = ""
    role: str = ""  # 哪个角色的接入：orchestrator 或专业节点名；空表示旧调用方未标明


_settings: ContextVar[ConnectionSettings | None] = ContextVar("ai_os_connection", default=None)
_on_route: ContextVar[Callable[[ConnectionSelection], None] | None] = ContextVar("ai_os_on_route", default=None)
# 专业节点分工的任务级覆盖：宿主在节点边界刷新快照，解析只读快照，因此在途节点不会中途换模型。
_role_settings: ContextVar[Callable[[], dict] | None] = ContextVar("ai_os_role_settings", default=None)
# 每次接入解析（AI OS 或专业节点）都交给宿主记录，页面与任务登记簿据此同步显示实际接入。
_route_recorder: ContextVar[Callable[[ConnectionSelection], None] | None] = ContextVar("ai_os_route_recorder", default=None)
_quota_cache: dict[str, tuple[float, "QuotaStatus"]] = {}
_quota_lock = threading.Lock()
ROLE_NAMES = ("orchestrator", "architect", "researcher", "writer", "reviewer", "stylist", "final_check")


def current_connection() -> ConnectionSettings | None:
    return _settings.get()


@contextmanager
def use_role_settings(reader: Callable[[], dict] | None, recorder: Callable[[ConnectionSelection], None] | None = None):
    """宿主（runner）绑定：reader 返回当前节点边界的分工覆盖快照；recorder 收每次实际接入。"""
    settings_token = _role_settings.set(reader)
    recorder_token = _route_recorder.set(recorder)
    try:
        yield
    finally:
        _route_recorder.reset(recorder_token)
        _role_settings.reset(settings_token)


def _notify_route(selected: "ConnectionSelection") -> None:
    for callback in (_on_route.get(), _route_recorder.get()):
        if callback:
            callback(selected)


def role_target(role: str) -> tuple[str, str]:
    """配置分工 → 任务级覆盖（TUI/MCP 设置，节点边界生效）。不核验额度。"""
    provider, model = config.ROLE_MODELS[role]
    reader = _role_settings.get()
    override = (reader() or {}).get(role) if reader else None
    if isinstance(override, dict) and override.get("provider"):
        return override["provider"], str(override.get("model") or "")
    return provider, model


def clear_quota_cache() -> None:
    with _quota_lock:
        _quota_cache.clear()


def _cached_quota(provider: str, manager: "ConnectionManager") -> "QuotaStatus":
    with _quota_lock:
        hit = _quota_cache.get(provider)
        if hit and 0 <= time.time() - hit[0] < config.ROLE_QUOTA_CACHE_S:
            return hit[1]
    probe = manager.codex_probe if provider == "codex" else manager.claude_probe
    try:
        quota = probe()
    except Exception:
        quota = QuotaStatus(provider, None, time.time(), f"{display_name(provider)} 额度读取失败")
    if not isinstance(quota, QuotaStatus):
        quota = QuotaStatus(provider, None, time.time(), f"{display_name(provider)} 额度未知")
    with _quota_lock:
        _quota_cache[provider] = (time.time(), quota)
    return quota


def resolve_role(role: str, *, manager: "ConnectionManager | None" = None) -> ConnectionSelection:
    """专业节点每次真实调用前解析实际接入：配置/覆盖 → 额度核验 → 回退链。

    切换从不静默：结果写日志、回报页面（on_route）并交宿主记录（route recorder）。
    strict 模式或 mock 模式只返回配置/覆盖的分工，不核验、不回退。
    """
    if role == "orchestrator":
        raise ValueError("AI OS 自身的接入由 resolve_connection 决定")
    provider, model = role_target(role)
    if config.MOCK_LLM:
        return ConnectionSelection(provider, model, role=role, reason="mock")
    if getattr(config, "ROLE_FALLBACK", "auto") != "auto":
        selected = ConnectionSelection(provider, model, role=role, reason="按配置分工（strict：不核验额度、不回退）")
        _notify_route(selected)
        return selected
    manager = manager or ConnectionManager()
    chain = [(provider, model)] + [(fallback, "") for fallback in config.ROLE_FALLBACKS.get(provider, ()) if fallback != provider]
    reasons, selected = [], None
    for index, (candidate, wanted) in enumerate(chain):
        note = "配置分工" if index == 0 else f"回退第 {index} 顺位"
        if candidate in {"codex", "claude"}:
            quota = _cached_quota(candidate, manager)
            if quota.provider == candidate and quota.permits():
                selected = ConnectionSelection(candidate, wanted, role=role, reason="；".join(reasons + [f"{note}：{quota.reason}"]))
                break
            reasons.append(quota.reason)
        elif candidate == "codebuddy":
            selected = ConnectionSelection(candidate, wanted, role=role, reason="；".join(reasons + [f"{note}：CodeBuddy 不提供额度查询，直接接入"]))
            break
        else:
            settings = config.PROVIDERS.get(candidate, {})
            if settings.get("api_key"):
                selected = ConnectionSelection(candidate, wanted or config.ROLE_FALLBACK_API_MODEL, str(settings.get("base_url", "")),
                                               settings["api_key"], "；".join(reasons + [f"{note}：接入 {display_name(candidate)}"]), role)
                break
            reasons.append(f"{display_name(candidate)} 未配置 API key")
    if selected is None:
        raise ConnectionError(f"{role} 没有可用接入，已保留断点：" + "；".join(reasons))
    from log import log
    log(f"[节点接入] {role} → {describe_selection(selected)}：{selected.reason}")
    _notify_route(selected)
    return selected


@contextmanager
def use_connection(settings: ConnectionSettings, on_route=None):
    """线程/异步任务隔离；新建线程的调用者须在该线程内进入此作用域。"""
    token = _settings.set(settings)
    route_token = _on_route.set(on_route)
    try:
        yield
    finally:
        _on_route.reset(route_token)
        _settings.reset(token)


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


WINDOW_NAMES = {"five_hour": "五小时窗口", "seven_day": "七天窗口", "spend_limit": "消费上限",
                "primary": "主窗口", "secondary": "次窗口"}


def _windows(provider, windows, observed_at, now, names=None):
    remaining, resets = [], []
    if not _number(observed_at) or not 0 <= now - observed_at <= 120:
        raise ValueError("过期或无效时间")
    for used, reset in windows:
        if not _number(used) or not 0 <= used <= 100:
            raise ValueError("额度不是有效百分比")
        if not _number(reset) or reset <= now:
            raise ValueError("窗口已过期或重置时间未知")
        remaining.append(100 - used)
        resets.append(reset)
    if not remaining:
        raise ValueError("没有额度窗口")
    value = min(remaining)
    detail = ""
    if names and len(names) == len(remaining):
        detail = "（" + "，".join(f"{WINDOW_NAMES.get(n, n)}已用 {100 - r:g}%" for n, r in zip(names, remaining)) + "）"
    return QuotaStatus(provider, value, observed_at, f"{display_name(provider)} 最低窗口剩余 {value:g}%{detail}", min(resets))


def parse_codex_quota(payload, *, observed_at=None, now=None) -> QuotaStatus:
    now = time.time() if now is None else now
    observed_at = now if observed_at is None else observed_at
    try:
        if not isinstance(payload, dict):
            raise ValueError()
        mapped = payload.get("rateLimitsByLimitId")
        if mapped is not None:
            if not isinstance(mapped, dict) or not mapped:
                raise ValueError()
            # 未获官方模型到bucket映射，保守取所有已返回窗口的最小剩余值。
            buckets = list(mapped.values())
        else:
            buckets = [payload.get("rateLimits")]
        windows, names = [], []
        for bucket in buckets:
            if not isinstance(bucket, dict):
                raise ValueError()
            if bucket.get("rateLimitReachedType"):
                return QuotaStatus("codex", 0, observed_at, "Codex 报告已达到额度上限")
            found = False
            for key in ("primary", "secondary"):
                window = bucket.get(key)
                if window is None:
                    continue
                if not isinstance(window, dict):
                    raise ValueError()
                found = True
                windows.append((window.get("usedPercent"), window.get("resetsAt")))
                names.append(key)
            if not found:
                raise ValueError()
        return _windows("codex", windows, observed_at, now, names)
    except (ValueError, TypeError):
        return QuotaStatus("codex", None, now, "Codex 额度未知、格式无效或已过期；禁止接入")


def parse_claude_quota(payload, *, now=None) -> QuotaStatus:
    now = time.time() if now is None else now
    try:
        if not isinstance(payload, dict):
            raise ValueError()
        limits = payload.get("rate_limits")
        if not isinstance(limits, dict):
            raise ValueError()
        # 官方订阅数据应同时提供五小时和七天窗口，缺一个不推定为无限。
        windows, names = [], []
        for key in ("five_hour", "seven_day"):
            if not isinstance(limits[key], dict):
                raise ValueError()
        # 其余已报告的窗口（如按模型或消费上限）同样计入最小值，不能只看两个总窗口。
        for key, window in limits.items():
            if window is None and key not in ("five_hour", "seven_day"):
                continue
            if not isinstance(window, dict):
                raise ValueError()
            windows.append((window.get("used_percentage"), window.get("resets_at")))
            names.append(key)
        return _windows("claude", windows, payload.get("observed_at"), now, names)
    except (ValueError, TypeError, KeyError, AttributeError):
        return QuotaStatus("claude", None, now, "Claude Code 额度未知、格式无效或已过期")


def probe_codex(*, timeout=15, environment=None) -> QuotaStatus:
    """只初始化并查询额度，不读材料，不发生成请求；错误输出不进入日志。"""
    from config import CLI_ENV
    proc = None
    try:
        proc = subprocess.Popen(command_for("codex") + ["app-server", "--listen", "stdio://"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", env=dict(CLI_ENV if environment is None else environment),
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        inbox = queue.Queue()

        def read_lines():
            try:
                for line in proc.stdout:
                    if len(line) <= 1_000_000:
                        inbox.put(line)
            finally:
                inbox.put(None)

        reader = threading.Thread(target=read_lines, daemon=True)
        reader.start()
        deadline = time.monotonic() + timeout

        def send(value):
            proc.stdin.write(json.dumps(value) + "\n")
            proc.stdin.flush()

        def receive(request_id):
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise TimeoutError()
                line = inbox.get(timeout=left)
                if line is None:
                    raise ValueError()
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError()
                if value.get("id") == request_id:
                    if "error" in value:
                        raise ValueError()
                    return value.get("result")

        send({"id": 1, "method": "initialize", "params": {
            "clientInfo": {"name": "writing_ai_os_quota", "version": "1.0"}}})
        receive(1)
        send({"method": "initialized", "params": {}})
        send({"id": 2, "method": "account/rateLimits/read"})
        return parse_codex_quota(receive(2))
    except (OSError, ValueError, TypeError, AgentError, TimeoutError, queue.Empty):
        return QuotaStatus("codex", None, time.time(), "无法读取 Codex 实时额度；禁止接入")
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)
            for stream in (proc.stdin, proc.stdout):
                if stream:
                    stream.close()


def _read_claude_quota(location) -> QuotaStatus:
    try:
        if not location:
            raise ValueError()
        target = Path(location)
        if target.stat().st_size > 16_384:
            raise ValueError()
        return parse_claude_quota(json.loads(target.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return QuotaStatus("claude", None, time.time(), "Claude Code 尚无新鲜额度数据")


def probe_claude(*, path: str | Path | None = None, live=None) -> QuotaStatus:
    """先读本地额度文件（statusline 或管道内 Claude 调用自报的额度）；不新鲜时向 Claude CLI 实时查询一次。

    不抓取/复制登录凭据。实时查询是一次最小请求，CLI 在其输出里自报账户额度窗口；
    WRITING_CLAUDE_QUOTA_PROBE=0 可关闭，关闭后没有新鲜数据即拒绝接入。
    """
    # 未显式指定时读仓库内的默认额度文件（已在 .gitignore）；新鲜度和窗口校验不变。
    location = path or os.getenv("WRITING_CLAUDE_QUOTA_FILE", "") or DEFAULT_CLAUDE_QUOTA_FILE
    quota = _read_claude_quota(location)
    if quota.remaining_percent is not None:
        return quota
    import config
    if live is None:
        live = os.getenv("WRITING_CLAUDE_QUOTA_PROBE", "1") != "0" and not config.MOCK_LLM and path is None
    if not live:
        return quota
    info = query_claude_rate_limit()
    if info is None:
        return QuotaStatus("claude", None, time.time(), "无法读取 Claude Code 实时额度；禁止接入")
    record_claude_rate_limit(info, Path(location))
    return _read_claude_quota(location)


def query_claude_rate_limit(*, timeout=60, environment=None):
    """最小请求只为取得 CLI 自报的 rate_limit_event；不带文章材料、工具或会话。计入模型请求预算。"""
    from config import CLI_ENV
    from service.model_budget import model_request
    try:
        with model_request("claude"):
            done = subprocess.run(command_for("claude") + ["-p", "--output-format", "stream-json", "--verbose",
                    "--model", os.getenv("WRITING_CLAUDE_QUOTA_PROBE_MODEL", "claude-haiku-4-5-20251001"),
                    "--tools", "", "--no-session-persistence", "--setting-sources", ""],
                input="只回复 OK", capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                env=dict(CLI_ENV if environment is None else environment),
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    except (OSError, subprocess.TimeoutExpired, AgentError):
        return None
    for line in done.stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type") == "rate_limit_event" and isinstance(event.get("rate_limit_info"), dict):
            return event["rate_limit_info"]
    return None


def record_claude_rate_limit(info, target: Path | None = None):
    """把 Claude CLI 自报的额度窗口写入同一份本地额度文件；只落数值，不落账户或会话字段。"""
    import config
    if not isinstance(info, dict):
        return
    if target is None:
        if config.MOCK_LLM:
            return
        target = Path(os.getenv("WRITING_CLAUDE_QUOTA_FILE", "") or DEFAULT_CLAUDE_QUOTA_FILE)
    limits = {}
    windows = info.get("unifiedWindows")
    if isinstance(windows, dict):
        for key, window in windows.items():
            if (isinstance(key, str) and re.fullmatch(r"[a-z0-9_]{1,40}", key) and isinstance(window, dict)
                    and _number(window.get("utilization"))):
                limits[key] = {"used_percentage": round(window["utilization"] * 100, 2),
                               "resets_at": window.get("resetsAt") if _number(window.get("resetsAt")) else None}
    kind = info.get("rateLimitType")
    if (info.get("status") not in ("allowed", "allowed_warning") and isinstance(kind, str)
            and re.fullmatch(r"[a-z0-9_]{1,40}", kind)):
        # CLI 明确报告该窗口已拒绝请求：按已用尽记录，不沿用可能滞后的百分比。
        limits[kind] = {"used_percentage": 100, "resets_at": info.get("resetsAt") if _number(info.get("resetsAt")) else None}
    if not limits:
        return
    _write_quota({"observed_at": time.time(), "source": "claude_cli_event", "rate_limits": limits}, target)


def _write_quota(safe: dict, target: Path):
    # 原子替换避免读取半个JSON；key、正文、账户名、会话路径均不落盘。
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + f".{os.getpid()}.{threading.get_ident()}.tmp")
    temp.write_text(json.dumps(safe), encoding="utf-8")
    temp.replace(target)


def capture_claude_quota(payload: dict, target: Path):
    """供 Claude statusline 调用：仅白名单额度字段落盘，不能保存完整会话JSON。"""
    limits = payload.get("rate_limits", {})
    safe = {"observed_at": time.time(), "source": "statusline", "rate_limits": {}}
    if isinstance(limits, dict):
        # 任何已报告的窗口都保留数值（含按模型或消费上限），键名只允许简单标识。
        for key, value in limits.items():
            if isinstance(key, str) and re.fullmatch(r"[a-z0-9_]{1,40}", key) and isinstance(value, dict):
                safe["rate_limits"][key] = {
                    name: value[name] if _number(value.get(name)) else None
                    for name in ("used_percentage", "resets_at")}
    _write_quota(safe, target)


class ConnectionManager:
    def __init__(self, codex_probe=None, claude_probe=None):
        self.codex_probe = codex_probe or probe_codex
        self.claude_probe = claude_probe or probe_claude

    def resolve(self, settings: ConnectionSettings) -> ConnectionSelection:
        import config
        reasons = []
        preferred = settings.provider
        candidates = ["codex", "claude"] if preferred == "auto" else ([preferred] if preferred in {"codex", "claude"} else [])
        for provider in candidates:
            try:
                quota = (self.codex_probe if provider == "codex" else self.claude_probe)()
            except Exception:
                quota = QuotaStatus(provider, None, time.time(), f"{display_name(provider)} 额度读取失败")
            if isinstance(quota, QuotaStatus) and quota.provider == provider and quota.permits():
                # auto 下填写的模型只应用于首选 Codex，不能把其名称传给 Claude。
                model = settings.model if preferred == provider or provider == "codex" else ""
                return ConnectionSelection(provider, model, reason="；".join(reasons + [quota.reason]))
            reasons.append(quota.reason if isinstance(quota, QuotaStatus) else f"{display_name(provider)} 额度未知")
            if preferred != "auto":
                threshold = 15 if provider == "codex" else 10
                raise ConnectionError(f"禁止接入 {display_name(provider)}：剩余额度须至少 {threshold}% 且可实时验证；" + "；".join(reasons))
        provider = "api" if preferred == "api" else "deepseek"
        defaults = config.PROVIDERS["deepseek"] if provider == "deepseek" else {}
        api_key = settings.api_key or defaults.get("api_key", "")
        base_url = settings.base_url or defaults.get("base_url", "")
        model = (settings.model if preferred == provider else "") or (
            config.ROLE_MODELS["architect"][1] if provider == "deepseek" else "")
        if not api_key or not base_url or not model:
            raise ConnectionError("；".join(reasons + ["API 接入需要 API key、地址和模型；密钥仅保留于当前进程"]))
        ConnectionSettings(provider=provider, base_url=base_url)
        return ConnectionSelection(provider, model, base_url, api_key,
            "；".join(reasons + ["接入 DeepSeek API" if provider == "deepseek" else "接入指定 API"]))


def describe_selection(selected: ConnectionSelection) -> str:
    """界面和日志显示实际接入的产品名与模型；CLI 未指定模型时如实写“CLI 默认模型”。"""
    model = selected.model or ("CLI 默认模型" if selected.provider in {"codex", "claude"} else "")
    return display_name(selected.provider) + (" / " + model if model else "")


def describe_settings(settings: ConnectionSettings | None = None) -> str:
    """尚未解析时只能说明选择顺序，不能预先声称已接入某一家。"""
    settings = settings or current_connection() or ConnectionSettings()
    if settings.provider == "auto":
        return "自动（Codex → Claude Code → DeepSeek API，调用时按额度决定）"
    return display_name(settings.provider)


def resolve_connection(settings=None) -> ConnectionSelection:
    selected = replace(ConnectionManager().resolve(settings or current_connection() or ConnectionSettings()), role="orchestrator")
    _notify_route(selected)
    return selected


def invoke_connection(system: str, user: str, *, settings=None, timeout=None, on_progress=None) -> AgentReply:
    """每次真实调用前重新检查额度；失败不把正文静默转交另一家。"""
    import config
    from agent_cli import invoke
    selected = resolve_connection(settings)
    from log import log
    log(f"[AI OS 接入] {describe_selection(selected)}：{selected.reason}")
    if selected.provider in {"codex", "claude"}:
        return invoke(selected.provider, selected.model, system + "\n\n" + user,
                      timeout=timeout or config.CLI_TIMEOUT_S, environment=config.CLI_ENV,
                      on_progress=on_progress)
    from openai import OpenAI
    from service.model_budget import BudgetExceeded, model_request
    from agent_cli import _EventProgress
    progress = _EventProgress(selected.provider, on_progress)
    try:
        progress.notify("api_started")
        with model_request(selected.provider), OpenAI(api_key=selected.api_key, base_url=selected.base_url,
                    timeout=timeout or config.LLM_READ_TIMEOUT_S, max_retries=0) as client:
            response = client.chat.completions.create(model=selected.model,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                **({"extra_body": config.PROVIDERS["deepseek"].get("extra_body", {})}
                   if selected.provider == "deepseek" else {}))
        text = response.choices[0].message.content
        if getattr(response.choices[0], "finish_reason", "stop") not in {None, "stop"}:
            raise ConnectionError("AI OS API 返回被截断或未完成的文本")
        if not isinstance(text, str) or not text.strip():
            raise ConnectionError("AI OS API 未返回完整文本")
        progress.events, progress.content_chars = 1, len(text)
        progress.notify("completed")
        return AgentReply(text, selected.model, response.model or selected.model, provider=selected.provider)
    except BudgetExceeded:
        raise
    except Exception:
        # SDK异常可能含请求头或响应正文，不能原样交给TUI/checkpoint。
        raise ConnectionError("AI OS API 调用失败；请检查接入地址、模型、密钥与账户额度") from None


if __name__ == "__main__":
    import sys
    if len(sys.argv) == 3 and sys.argv[1] == "--capture-claude-quota":
        # Claude Code 以 UTF-8 传入 JSON（含会话路径，可能有中文）；Windows 默认按本地编码读写会解码失败或输出乱码。
        capture_claude_quota(json.loads(sys.stdin.buffer.read().decode("utf-8")), Path(sys.argv[2]))
        sys.stdout.buffer.write("AI OS 额度已同步".encode("utf-8"))
    else:
        raise SystemExit("用法：python -m ai_os_connection --capture-claude-quota <额度文件路径>")
