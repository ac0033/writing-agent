"""CLI 文本调用：独立上下文、显式权限、完整结果校验；不做静默模型回退。"""
from __future__ import annotations

import json
import re
import os
import shutil
import subprocess
import tempfile
import hashlib
import uuid
import queue
import threading
import time
import signal
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path


class AgentError(RuntimeError):
    pass


@dataclass
class AgentReply:
    text: str
    requested_model: str
    actual_model: str
    session_id: str = ""
    provider: str = ""


# 界面和日志使用产品名称；provider 标识只用于配置与路由。
DISPLAY_NAMES = {"codex": "Codex", "claude": "Claude Code", "codebuddy": "CodeBuddy",
                 "deepseek": "DeepSeek API", "api": "自定义 API", "qwen": "千问 API"}


def display_name(provider: str) -> str:
    return DISPLAY_NAMES.get(provider, provider)


def _record_claude_rate_limit(info):
    """只转交数值型窗口；写入失败不能影响模型调用。"""
    try:
        from ai_os_connection import record_claude_rate_limit
        record_claude_rate_limit(info)
    except Exception:
        pass


def failure_categories(output: str) -> list[str]:
    """从失败输出归类原因。Claude 每次输出都含 rate_limit_event 字样，不能拿它当额度用尽的证据。"""
    lowered = output.lower()
    found = [label for label, terms in {
        "runtime_panic": ("panic:", "bun has crashed", "segmentation fault", "stack overflow"),
        "memory": ("out of memory", "memory allocation", "allocation failed"),
        "context": ("prompt is too long", "context length exceeded"),
        # 订阅额度用尽时续跑同一家只会再失败一次并多记一次预算，需换接入或等额度恢复。
        "usage_limit": ("usage limit", "usage_limit_exceeded", "quota exceeded", "insufficient balance"),
    }.items() if any(term in lowered for term in terms)]
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        for item in event if isinstance(event, list) else [event]:
            if not isinstance(item, dict):
                continue
            info = item.get("rate_limit_info")
            if item.get("type") == "rate_limit_event" and isinstance(info, dict) and info.get("status") not in (None, "allowed", "allowed_warning"):
                found.append("usage_limit")
            if item.get("type") == "result" and item.get("is_error"):
                # 只取子类型和HTTP状态码，不带可能含账户信息的原文。
                subtype = item.get("subtype")
                if isinstance(subtype, str) and re.fullmatch(r"[a-z_]{1,40}", subtype):
                    found.append("result_" + subtype)
                status = re.search(r"API Error:?\s*(\d{3})", str(item.get("result", "")))
                if status:
                    found.append("api_" + status.group(1))
    return list(dict.fromkeys(found))


class _EventProgress:
    """回调只接收统计，不接收正文、工具参数、推理、账户字段或原始事件。"""
    def __init__(self, provider, callback):
        self.provider, self.callback = provider, callback
        self.events = self.content_chars = self.tool_calls = self.tool_results = 0
        self._texts = {}
        self._tools = set()
        self._results = set()
        self._stream_message = ""

    def notify(self, phase):
        if self.callback:
            try:
                self.callback({"phase": phase, "events": self.events,
                    "content_chars": self.content_chars, "tool_calls": self.tool_calls,
                    "tool_results": self.tool_results})
            except Exception:
                # 观测失败不能重试模型、影响正文或把回调异常中的秘密写入日志。
                pass

    def _text(self, key, text, delta=False):
        if not isinstance(text, str):
            return
        old = self._texts.get(key, 0)
        size = old + len(text) if delta else max(old, len(text))
        self._texts[key] = size
        self.content_chars += size - old

    def _tool(self, key, result=False):
        seen = self._results if result else self._tools
        if key not in seen:
            seen.add(key)
            if result:
                self.tool_results += 1
            else:
                self.tool_calls += 1

    def accept(self, value):
        if isinstance(value, list):
            for event in value:
                self.accept(event)
            return
        if not isinstance(value, dict):
            return
        self.events += 1
        kind = value.get("type")
        if not isinstance(kind, str):
            kind = ""
        phase = "event"
        if self.provider == "codex":
            item = value.get("item") or {}
            if isinstance(item, dict):
                key = item.get("id") or "last-agent-message"
                if item.get("type") == "agent_message":
                    self._text(str(key), item.get("text"))
                if isinstance(item.get("type"), str) and item.get("type") in {"command_execution", "mcp_tool_call", "web_search", "file_change"}:
                    self._tool(str(key))
                    if kind == "item.completed":
                        self._tool(str(key), result=True)
        else:
            if kind == "stream_event":
                event = value.get("event") or {}
                if isinstance(event, dict):
                    message = event.get("message") or {}
                    if event.get("type") == "message_start" and isinstance(message, dict):
                        self._stream_message = str(message.get("id") or self.events)
                    key = self._stream_message or "stream"
                    delta = event.get("delta") or {}
                    if isinstance(delta, dict) and delta.get("type") == "text_delta":
                        self._text(key, delta.get("text"), delta=True)
                    block = event.get("content_block") or {}
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        self._tool(str(block.get("id") or f"{key}:{event.get('index')}"))
            message = value.get("message") or {}
            if isinstance(message, dict):
                blocks = message.get("content") or []
                if isinstance(blocks, list):
                    texts = []
                    for index, block in enumerate(blocks):
                        if not isinstance(block, dict):
                            continue
                        if kind == "assistant" and block.get("type") == "text" and isinstance(block.get("text"), str):
                            texts.append(block["text"])
                        if block.get("type") == "tool_use":
                            self._tool(str(block.get("id") or f"{self.events}:{index}"))
                        elif block.get("type") == "tool_result":
                            self._tool(str(block.get("tool_use_id") or f"{self.events}:{index}"), result=True)
                    if texts:
                        self._text(str(message.get("id") or self._stream_message or self.events), "".join(texts))
        if kind == "rate_limit_event" and self.provider == "claude":
            # Claude CLI 每次调用都自报账户额度窗口；这是比 statusline 更及时的权威来源。
            _record_claude_rate_limit(value.get("rate_limit_info"))
        if kind == "result":
            # 结果通常重复最后一条assistant消息，不能把同一正文计两遍。
            result = value.get("result")
            if not self.content_chars and isinstance(result, str):
                self._text("result", result)
            phase = "complete_event" if value.get("subtype") == "success" else "error_event"
        elif kind == "turn.completed":
            phase = "complete_event"
        elif kind in {"error", "turn.failed"}:
            phase = "error_event"
        self.notify(phase)


def _stop_process(proc):
    """只回收本次启动的进程树；超时不留下继续消耗额度的 CLI。"""
    if proc.poll() is None:
        if os.name == "nt":
            try:
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5,
                    creationflags=subprocess.CREATE_NO_WINDOW)
            except (OSError, subprocess.TimeoutExpired):
                pass
        else:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if proc.poll() is None:
            proc.kill()
    proc.wait(timeout=5)


def _stream_process(cmd, *, input, cwd, timeout, env, progress):
    """并发消费管道，主线程执行进度回调；大stdin或大量stderr不能阻塞超时。"""
    from service.cancellation import current_cancellation, check_cancelled
    check_cancelled()
    proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="strict",
        creationflags=(subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == "nt" else 0,
        start_new_session=os.name != "nt")
    from service.process_job import bind_process
    try:
        close_job = bind_process(proc)
    except OSError:
        _stop_process(proc)
        raise AgentError("CLI 回收作用域初始化失败，未发送输入") from None
    cancellation = current_cancellation.get()
    if cancellation is not None:
        cancellation.track_process(proc)
    progress.notify("process_started")
    inbox = queue.Queue()
    stdout, stderr = [], []
    stderr_size = stdout_size = 0
    readers = []

    def read(stream, channel):
        try:
            while chunk := stream.readline(8 * 1024 * 1024 + 1):
                inbox.put((channel, chunk))
        except (UnicodeError, OSError, ValueError) as exc:
            inbox.put(("read_error", type(exc).__name__))
        finally:
            inbox.put(("closed", channel))

    def write():
        try:
            proc.stdin.write(input)
            proc.stdin.close()
        except (BrokenPipeError, OSError, UnicodeError, ValueError):
            # 子进程可能先返回鉴权错误，仍须消费其退出码与完整输出。
            pass

    for stream, channel in ((proc.stdout, "stdout"), (proc.stderr, "stderr")):
        thread = threading.Thread(target=read, args=(stream, channel), daemon=True)
        thread.start()
        readers.append(thread)
    writer = threading.Thread(target=write, daemon=True)
    writer.start()
    deadline = time.monotonic() + timeout
    closed = set()
    try:
        while len(closed) < 2 or proc.poll() is None:
            check_cancelled()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(cmd, timeout)
            try:
                channel, chunk = inbox.get(timeout=min(0.1, remaining))
            except queue.Empty:
                continue
            if channel == "closed":
                closed.add(chunk)
            elif channel == "read_error":
                raise AgentError("CLI 输出编码或读取失败")
            elif channel == "stderr":
                stderr_size += len(chunk.encode("utf-8"))
                # 只保留有限诊断片段于内存，不发给进度回调，不落盘。
                if sum(map(len, stderr)) < 65536:
                    stderr.append(chunk[:65536])
            else:
                stdout_size += len(chunk.encode("utf-8"))
                if len(chunk) > 8 * 1024 * 1024 or stdout_size > 32 * 1024 * 1024:
                    raise AgentError("CLI 输出超过安全大小上限")
                stdout.append(chunk)
                try:
                    progress.accept(json.loads(chunk))
                except (ValueError, TypeError, AttributeError):
                    # 非JSON/多行兼容输出仍由最终parse_output严格判定。
                    pass
        check_cancelled()
        result = subprocess.CompletedProcess(cmd, proc.returncode, "".join(stdout), "".join(stderr))
        result.stderr_bytes = stderr_size
        return result
    finally:
        if close_job is not None:
            close_job()
        _stop_process(proc)
        writer.join(timeout=1)
        for reader in readers:
            reader.join(timeout=1)
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream and not stream.closed:
                stream.close()
        if cancellation is not None:
            cancellation.untrack_process(proc)


def command_for(provider: str) -> list[str]:
    """JSON 数组覆盖支持 node + 脚本路径，不经 shell 解释。"""
    key = provider.upper() + "_COMMAND_JSON"
    if os.getenv(key):
        value = json.loads(os.environ[key])
        if not isinstance(value, list) or not value or not all(isinstance(x, str) and x for x in value):
            raise AgentError(f"{key} 必须是非空字符串数组")
        return value
    executable = {"claude": "claude", "codex": "codex", "codebuddy": "codebuddy"}[provider]
    found = shutil.which(executable)
    if found:
        return [found]
    if provider == "codex" and os.name == "nt":
        # Codex Desktop 自带 CLI 但不写入 PATH；只有在 App 内启动的进程才继承得到。
        # 普通终端启动 TUI 时从安装目录发现，多版本并存取最近更新的一份。
        root = Path(os.getenv("LOCALAPPDATA", "")) / "OpenAI/Codex/bin"
        bundled = sorted(root.glob("*/codex.exe"), key=lambda p: p.stat().st_mtime, reverse=True) if root.is_dir() else []
        if bundled:
            return [str(bundled[0])]
    if provider == "codebuddy":
        # 优先显式路径，其次查询安装登记；不读取或复制 WorkBuddy 凭据。
        roots = [os.getenv("WORKBUDDY_INSTALL_DIR", "")]
        if os.name == "nt":
            import winreg
            for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                try:
                    with winreg.OpenKey(hive, r"Software\Microsoft\Windows\CurrentVersion\Uninstall") as parent:
                        for i in range(winreg.QueryInfoKey(parent)[0]):
                            with winreg.OpenKey(parent, winreg.EnumKey(parent, i)) as child:
                                try:
                                    name = winreg.QueryValueEx(child, "DisplayName")[0]
                                    icon = winreg.QueryValueEx(child, "DisplayIcon")[0]
                                    if name.startswith("WorkBuddy"):
                                        roots.append(str(Path(icon.rsplit(",", 1)[0].strip('"')).parent))
                                except OSError:
                                    continue
                except OSError:
                    continue
        for root in filter(None, roots):
            entry = Path(root) / "resources/app.asar.unpacked/cli/bin/codebuddy"
            node = shutil.which("node")
            if entry.is_file() and node:
                return [node, str(entry)]
    raise AgentError(f"找不到 {provider}；请安装 CLI 或设置 {key}")


def parse_output(provider: str, output: str, requested: str) -> AgentReply:
    """兼容 Claude 单对象、CodeBuddy 数组和 Codex JSONL；中间文本不是成功结果。"""
    try:
        value = json.loads(output)
        events = value if isinstance(value, list) else [value]
    except ValueError:
        try:
            events = [json.loads(line) for line in output.splitlines() if line.strip()]
        except ValueError as exc:
            raise AgentError(f"{provider} 输出不是完整 JSON") from exc
    answer, actual, session, complete = "", "", "", False
    for event in events:
        if not isinstance(event, dict):
            raise AgentError(f"{provider} 事件格式无效")
        kind = event.get("type")
        if not isinstance(kind, str):
            raise AgentError(f"{provider} 事件类型无效")
        if kind in {"error", "turn.failed"} or event.get("is_error"):
            raise AgentError(f"{provider} 返回失败事件；没有有效最终结果")
        metadata = event.get("providerData") or {}
        actual = metadata.get("model") or event.get("model") or actual
        session = event.get("session_id") or event.get("thread_id") or session
        if provider == "codex":
            item = event.get("item") or {}
            if not isinstance(item, dict):
                raise AgentError(f"{provider} 事件内容格式无效")
            if kind == "turn.started":
                complete, answer = False, ""
            if kind == "item.completed" and item.get("type") == "agent_message":
                answer = item.get("text", "")
            if kind == "turn.completed":
                complete = True
        elif kind == "result":
            if event.get("subtype") != "success":
                raise AgentError(f"{provider} 未成功完成：返回非 success 状态")
            if event.get("permission_denials"):
                raise AgentError(f"{provider} 存在被拒绝的工具操作，请检查权限后重试")
            structured = event.get("structured_output")
            answer = (json.dumps(structured, ensure_ascii=False)
                      if provider == "claude" and isinstance(structured, dict)
                      else event.get("result", ""))
            complete = True
            models = event.get("modelUsage", {})
            if len(models) == 1 and not actual:
                actual = next(iter(models))
        elif kind == "assistant":
            complete = False
    if not complete or not isinstance(answer, str) or not answer.strip():
        raise AgentError(f"{provider} 没有完整、非空的最终结果")
    return AgentReply(answer, requested, actual or "未报告", session, provider)


def invoke(provider: str, model: str, prompt: str, *, timeout: int = 900,
           cwd: Path | None = None, file_tools: bool = False,
           writable: tuple[str, ...] = (), runner=None,
           environment: dict[str, str] | None = None,
           mcp_config: Path | None = None,
           mcp_tools: tuple[str, ...] = (), response_schema: dict | None = None,
           on_progress=None) -> AgentReply:
    """默认不开放工具。wiki 仅开放读和指定路径的写；绝不开放 shell。"""
    if provider not in {"claude", "codex", "codebuddy"}:
        raise AgentError(f"不支持的 CLI：{provider}")
    if response_schema is not None and provider != "claude":
        raise AgentError("原生JSON Schema目前仅用于Claude文本节点")
    if mcp_config and provider != "claude":
        raise AgentError("记忆 MCP 目前仅验证了 Claude 入口，不向其他入口静默套用配置")
    # Windows CLI 的短生命周期子进程可能暂时持有 cwd 句柄；清理失败不能吞掉已完成结果。
    with tempfile.TemporaryDirectory(prefix="llm-cli-", ignore_cleanup_errors=True) as scratch:
        cmd = command_for(provider)
        if provider == "codex":
            if writable:
                raise AgentError("Codex 在此适配器中只用于只读核验")
            cmd += ["exec", "--json", "--ephemeral", "--skip-git-repo-check",
                    "--sandbox", "read-only", "--ignore-user-config", "-"]
            if model:
                cmd += ["--model", model]
        else:
            cmd += ["-p", "--output-format", "json", "--no-session-persistence",
                    "--strict-mcp-config", "--permission-mode", "dontAsk",
                    "--setting-sources", "", "--settings", '{"disableAllHooks":true,"autoMemoryEnabled":false}']
            if model:
                cmd += ["--model", model]
            if provider == "claude":
                cmd[cmd.index("--output-format") + 1] = "stream-json"
                cmd += ["--verbose", "--include-partial-messages"]
                cmd += ["--permission-prompts", "none"]
                if response_schema is not None:
                    cmd += ["--json-schema", json.dumps(response_schema, ensure_ascii=False)]
            names = ["Read", "Glob", "Grep"] if file_tools else []
            rules = list(names)
            if mcp_config:
                cmd += ["--mcp-config", str(mcp_config.resolve())]
                rules += list(mcp_tools)
            if writable:
                names += ["Edit", "Write"]
                for path in writable:
                    rules += [f"Edit({path})", f"Write({path})"]
                cmd += ["--disallowedTools", "Edit(./wiki/index.md),Write(./wiki/index.md)"]
            cmd += ["--tools", ",".join(names)]
            if rules:
                cmd += ["--allowedTools", ",".join(rules)]
        env = dict(os.environ if environment is None else environment)
        env["DISABLE_AUTOUPDATER"] = "1"
        env["CODEBUDDY_DISABLE_COMPILE_CACHE"] = "1"
        # 嵌套调用是新的一次独立任务，不向 CLI 传递父会话标识。
        env.pop("CLAUDECODE", None)
        progress = _EventProgress(provider, on_progress)
        try:
            from service.model_budget import model_request
            with model_request(provider):
                if runner:
                    proc = runner(cmd, input=prompt, cwd=cwd or scratch,
                        capture_output=True, text=True, encoding="utf-8", errors="strict",
                        timeout=timeout, env=env)
                else:
                    proc = _stream_process(cmd, input=prompt, cwd=cwd or scratch,
                        timeout=timeout, env=env, progress=progress)
        except subprocess.TimeoutExpired:
            progress.notify("failed")
            raise AgentError(f"{provider} 调用超时：timeout={timeout}s；输入UTF8字节={len(prompt.encode('utf-8'))}") from None
        except (OSError, UnicodeError) as exc:
            progress.notify("failed")
            raise AgentError(f"{provider} 调用失败：{type(exc).__name__}") from None
        if proc.returncode:
            # 不把可能含账户信息的原始 stderr 放进用户日志。
            output = proc.stderr + '\n' + proc.stdout
            categories = failure_categories(output)
            detail = f"；诊断类别={','.join(categories) or 'unknown'}；输入UTF8字节={len(prompt.encode('utf-8'))}；stderr字节={getattr(proc, 'stderr_bytes', len(proc.stderr.encode('utf-8')))}"
            progress.notify("failed")
            raise AgentError(f"{provider} 退出码 {proc.returncode}" + detail)
        try:
            reply = parse_output(provider, proc.stdout, model)
        except AgentError:
            progress.notify("failed")
            raise
        progress.notify("completed")
        trace_dir = env.get("LLM_CLI_TRACE_DIR")
        if trace_dir:
            # 留调用证据，不保存提示词、工具参数、召回内容或模型 thinking。
            try:
                events = json.loads(proc.stdout)
                events = events if isinstance(events, list) else [events]
            except ValueError:
                events = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
            calls, results = [], []
            for event in events:
                for block in (event.get("message") or {}).get("content", []):
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "tool_use":
                        calls.append({"id": block.get("id"), "name": block.get("name")})
                    elif block.get("type") == "tool_result":
                        results.append({"id": block.get("tool_use_id"), "is_error": bool(block.get("is_error"))})
            record = {"at": datetime.now(timezone.utc).isoformat(), "provider": provider,
                      "requested_model": model, "actual_model": reply.actual_model,
                      "session_id": reply.session_id, "tools": calls, "tool_results": results,
                      "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                      "result_sha256": hashlib.sha256(reply.text.encode()).hexdigest()}
            folder = Path(trace_dir)
            folder.mkdir(parents=True, exist_ok=True)
            (folder / (uuid.uuid4().hex + ".json")).write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        return reply
