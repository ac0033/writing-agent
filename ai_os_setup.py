"""AI OS 接入页的后台逻辑：检测本机 CLI、把手写的模型标识规范成接入方认可的写法、记住上次选择（不含密钥）。

本项目开源，使用者本机不一定装了 Codex / Claude Code，所以接入页先检测再给选项；
检测只跑 `--version` 与 Codex 的只读 model/list，不发生成请求、不读取或复制登录凭据。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import difflib
import json
import os
from pathlib import Path
import re
import subprocess
import unicodedata

import config
from agent_cli import AgentError, command_for, display_name
from ai_os_connection import ConnectionError, ConnectionSettings, list_codex_models

CHOICE_FILE = "ai_os_choice.json"
_VENDOR_PREFIXES = ("anthropic/", "openai/", "claude-", "gpt-", "deepseek-")
_CLAUDE_ALIASES = {"opus", "sonnet", "haiku", "default"}


@dataclass(frozen=True)
class CliInfo:
    provider: str
    available: bool
    version: str = ""
    reason: str = ""
    models: tuple = ()  # ((标识, 显示名, 是否默认), ...)，只有 Codex 有

    @property
    def default_model(self) -> str:
        return next((model for model, _, default in self.models if default), "")


def detect_cli(provider: str, *, timeout: int = 10) -> CliInfo:
    try:
        command = command_for(provider)
    except AgentError:
        return CliInfo(provider, False, reason="未检测到（未安装或不在 PATH）")
    try:
        done = subprocess.run(command + ["--version"], capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout, stdin=subprocess.DEVNULL, env=dict(config.CLI_ENV),
                              creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    except (OSError, subprocess.TimeoutExpired):
        return CliInfo(provider, False, reason="已找到但无法运行")
    lines = (done.stdout or "").strip().splitlines()
    if done.returncode != 0 or not lines:
        return CliInfo(provider, False, reason="已找到但无法运行")
    if provider != "codex":
        return CliInfo(provider, True, lines[0], "使用本机登录，可自动接入")
    models = tuple(list_codex_models())
    reason = ("使用本机登录，可自动接入" if models
              else "已安装，但读不到模型列表（可能未登录：codex login）")
    return CliInfo(provider, True, lines[0], reason, models)


def detect_clis() -> dict[str, CliInfo]:
    with ThreadPoolExecutor(max_workers=2) as pool:
        found = {provider: pool.submit(detect_cli, provider) for provider in ("codex", "claude")}
        return {provider: future.result() for provider, future in found.items()}


def claude_catalog() -> tuple[str, ...]:
    configured = [model for provider, model in config.ROLE_MODELS.values() if provider == "claude" and model]
    return tuple(dict.fromkeys([*config.AI_OS_CLAUDE_MODELS, *configured]))


def _clean(raw: str) -> str:
    # NFKC 把全角字母数字、全角空格折成半角；再去掉用户常顺手带上的引号。
    text = unicodedata.normalize("NFKC", raw or "").strip().strip("\"'`“”‘’ ")
    return re.sub(r"\s+", " ", text)


def _key(text: str) -> str:
    # 字母后紧跟数字时补连字符（sonnet5 → sonnet-5、gpt6 → gpt-6）；目录两侧用同一规则，不影响命中。
    key = re.sub(r"(?<=[a-z])(?=\d)", "-", re.sub(r"[\s_.]+", "-", text.lower()))
    return re.sub(r"-+", "-", key).strip("-")


def _bare(key: str) -> str:
    for prefix in _VENDOR_PREFIXES:
        if key.startswith(prefix):
            return key[len(prefix):]
    return key


def normalize_model(provider: str, raw: str, catalog=(), authoritative: bool = False) -> tuple[str, str]:
    """返回 (规范标识, 说明)。留空返回 ("", "")，表示用接入方默认模型。

    比较时统一大小写、空格/下划线/点号和厂商前缀，命中目录就用目录里的原样写法；
    authoritative=True（目录是接入方实时返回的）时，目录外的输入直接拒绝并给出相近候选。
    """
    text = _clean(raw)
    if not text:
        return "", ""
    key = _key(text)
    table = {}
    for model in catalog:
        for variant in (_key(model), _bare(_key(model))):
            table.setdefault(variant, model)

    def changed(model, extra=""):
        return model, (f"“{text}”已规范为 {model}" if model != text else "") + extra

    if key in table or _bare(key) in table:
        return changed(table.get(key) or table[_bare(key)])
    partial = sorted({model for model in catalog if _bare(key) in _key(model)})
    if len(partial) == 1:
        return changed(partial[0])
    if len(partial) > 1:
        raise ConnectionError(f"“{text}”对应多个模型：{'、'.join(partial)}；请写完整")
    if authoritative:
        keys = {_key(model): model for model in catalog}
        close = [keys[k] for k in difflib.get_close_matches(key, list(keys), n=3, cutoff=0.5)]
        listed = "；是不是：" + "、".join(close) if close else "；可选：" + "、".join(list(catalog)[:6])
        raise ConnectionError(f"{display_name(provider)} 里没有“{text}”{listed}")
    if provider == "claude":
        model = key if key.startswith("claude-") or key in _CLAUDE_ALIASES else "claude-" + key
        if not re.fullmatch(r"claude-[a-z]+(-\d+)+(-\d{8})?", model) and model not in _CLAUDE_ALIASES:
            raise ConnectionError(f"“{text}”不像 Claude 模型标识；示例：{'、'.join(claude_catalog()[:4])}")
        return changed(model, "（不在已知目录，首次调用时由 Claude Code 校验）")
    model = text.replace(" ", "-")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/\-]*", model):
        raise ConnectionError("模型标识只能包含字母、数字和 . _ : / -")
    return changed(model, "（未能读取接入方模型列表，首次调用时校验）")


API_PROVIDERS = ("deepseek", "api", "anthropic")


def list_api_models(base_url: str, api_key: str, *, timeout: int = 10) -> list[str] | None:
    """OpenAI 兼容 /models；密钥错误明确报出，其他失败返回 None（有些服务不开放该接口）。"""
    from openai import AuthenticationError, OpenAI
    try:
        with OpenAI(api_key=api_key, base_url=base_url, timeout=timeout, max_retries=0) as client:
            return sorted(model.id for model in client.models.list())
    except AuthenticationError:
        raise ConnectionError("API key 无效（接入方返回 401）") from None
    except Exception:
        return None


def list_anthropic_models(base_url: str, api_key: str, *, timeout: int = 10) -> list[str] | None:
    """Anthropic 兼容 GET /v1/models（SDK 自动翻页）；很多兼容服务不开放该接口，失败返回 None。"""
    import anthropic
    from ai_os_connection import anthropic_base_url
    try:
        with anthropic.Anthropic(api_key=api_key, base_url=anthropic_base_url(base_url),
                                 timeout=timeout, max_retries=0) as client:
            return sorted(model.id for model in client.models.list())
    except anthropic.AuthenticationError:
        raise ConnectionError("API key 无效（接入方返回 401）") from None
    except Exception:
        return None


def api_endpoint(provider: str, base_url: str, api_key: str) -> tuple[str, str]:
    """实际要用的地址与密钥：DeepSeek 留空时取 .env 与内置地址，其余两种必须由用户填写。"""
    defaults = config.PROVIDERS["deepseek"] if provider == "deepseek" else {}
    url = ((base_url or "").strip() if provider != "deepseek" else "") or defaults.get("base_url", "")
    return url, (api_key or "").strip() or defaults.get("api_key", "")


def fetch_models(provider: str, base_url: str = "", api_key: str = "") -> list[str] | None:
    """第二步“可选模型”列表：API 接入实时读取；读不到返回 None，页面改为手动输入。模拟模式不联网。"""
    if config.MOCK_LLM:
        return None
    url, key = api_endpoint(provider, base_url, api_key)
    if not url or not key:
        return None
    ConnectionSettings(provider=provider, base_url=url)  # 先校验地址格式，再去联网
    return (list_anthropic_models if provider == "anthropic" else list_api_models)(url, key)


def prepare_connection(choice: dict, clis: dict[str, CliInfo]) -> tuple[ConnectionSettings, list[str]]:
    """把接入页的原始输入变成可用的 ConnectionSettings；只在页面校验时调用一次，不发生成请求。

    choice["models"] 是第二步已读到的 API 模型列表（没读到为 None），避免同一次接入重复联网。
    """
    provider = choice.get("provider", "")
    notes: list[str] = []

    def available(name):
        info = clis.get(name)
        return info is not None and info.available

    def cli_model(name, raw):
        info = clis.get(name)
        catalog = [model for model, _, _ in info.models] if name == "codex" and info else list(claude_catalog())
        model, note = normalize_model(name, raw, catalog, authoritative=name == "codex" and bool(catalog))
        if note:
            notes.append(note)
        return model

    if provider in {"codex", "claude"}:
        if not available(provider):
            raise ConnectionError(f"本机未检测到 {display_name(provider)}，请换一种接入方式")
        return ConnectionSettings(provider=provider, model=cli_model(provider, choice.get("model", ""))), notes
    if provider == "auto":
        if not config.AI_OS_AUTO_ENABLED:
            raise ConnectionError("自动模式未在本机启用")
        if not (available("codex") or available("claude")):
            raise ConnectionError("本机没有可用的 Codex 或 Claude Code，自动模式无从接入；请选 API 接入")
        return ConnectionSettings(provider="auto",
            model=cli_model("codex", choice.get("model", "")) if available("codex") else "",
            claude_model=cli_model("claude", choice.get("claude_model", "")) if available("claude") else ""), notes
    if provider not in API_PROVIDERS:
        raise ConnectionError("请选择接入方式")
    base_url = (choice.get("base_url") or "").strip() if provider != "deepseek" else ""
    api_key = (choice.get("api_key") or "").strip()
    url, key = api_endpoint(provider, base_url, api_key)
    raw = choice.get("model", "") or (config.ROLE_MODELS["architect"][1] if provider == "deepseek" else "")
    if not key:
        raise ConnectionError("请填写 API key（只在本次进程内使用，不落盘）")
    if not url:
        raise ConnectionError("请填写 API 地址")
    if not _clean(raw):
        raise ConnectionError("请选择或填写模型")
    models = choice["models"] if "models" in choice else fetch_models(provider, base_url, api_key)
    catalog = models or ([config.ROLE_MODELS["architect"][1]] if provider == "deepseek" else [])
    model, note = normalize_model(provider, raw, catalog, authoritative=bool(models))
    if note:
        notes.append(note)
    return ConnectionSettings(provider=provider, model=model, base_url=base_url, api_key=api_key), notes


def load_choice(directory: Path) -> dict:
    try:
        value = json.loads((Path(directory) / CHOICE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(value, dict):
        return {}
    return {key: str(value.get(key, "")) for key in ("provider", "model", "claude_model", "base_url")}


def save_choice(directory: Path, settings: ConnectionSettings) -> None:
    """只记接入方式、模型和地址，方便下次预选；密钥绝不写入。"""
    target = Path(directory) / CHOICE_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"provider": settings.provider, "model": settings.model,
                                  "claude_model": settings.claude_model, "base_url": settings.base_url},
                                 ensure_ascii=False, indent=2), encoding="utf-8")
