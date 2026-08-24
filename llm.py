"""LLM 调用封装：多 provider + 统一的"思考/结果"两段式输出契约。

接口规范（所有节点一致）：
    模型输出必须分两段——
    <scratchpad>
    自由推理：分析输入、权衡方案、自我质疑。这段不进下游、不进发布稿，只进思考日志。
    </scratchpad>
    <result>
    该节点约定的结构化结果（各节点 prompt 定义 <result> 内部的字段格式）。
    </result>

设计理由：
- 不用各家原生 thinking 的输出做解析（deepseek 的 reasoning_content / qwen 的
  enable_thinking 格式不统一、不可跨 provider 解析）；统一在 content 里打标记，
  任何模型都遵守同一份契约，scratchpad 还能落进 state 供回溯（白盒）。
- 原生 thinking 默认关闭，仅对 config.THINKING_ROLES 中的质量敏感角色开启
  （A/B 实测开启后成稿质量明显提升）。原生思考只作为 scratchpad 之外的增强，
  解析仍然只认 content 里的契约标记，两者互不干扰。
  注意：qwen 开思考后不要设过小的 max_tokens，否则会触发
  max_tokens < thinking_budget 的 400（当前代码不设 max_tokens）。

错误处理（见 _create）：
- 瞬时错误（429 限流、5xx、网络/超时）：指数退避重试，多数能自愈；
- 不可重试的错误（余额不足、key 无效等）：转成带排查指引的 RuntimeError。
  节点代码不捕获异常——失败后用相同 --thread-id 重跑即可从断点继续。
"""
import os
import re
import threading
import time
from dataclasses import dataclass
from types import SimpleNamespace

from openai import APIConnectionError, APIStatusError, OpenAI

import config
from log import heartbeat, log

# 追加在每个 system prompt 末尾的输出契约
CONTRACT = """

---

## 输出契约（必须严格遵守）

你的输出必须且只能包含以下两段，顺序固定：

<scratchpad>
在这里自由思考：分析输入材料、权衡不同方案、指出疑点、推演结论。
这段内容不会进入下游节点和最终稿件，只用于过程留痕。想到什么写什么，不用顾忌格式。
</scratchpad>

<result>
这里放你的正式产出，内部格式按上文的角色要求执行。
</result>
"""

_clients: dict[str, OpenAI] = {}


@dataclass
class ChatResult:
    thinking: str   # <scratchpad> 内容
    result: str     # <result> 内容
    raw: str        # 原始输出
    model: str      # 实际调用的模型名
    parsed_ok: bool # 是否成功按契约解析（False = 回退为全文）


def _get_client(provider: str) -> OpenAI:
    if provider not in _clients:
        cfg = config.PROVIDERS[provider]
        if not cfg["api_key"]:
            raise RuntimeError(f"未找到 {provider} 的 API key，请在 {config.ENV_PATH} 中配置。")
        _clients[provider] = OpenAI(api_key=cfg["api_key"], base_url=cfg["base_url"])
    return _clients[provider]


MAX_RETRIES = 3          # 瞬时错误的重试次数（首发不算）
RETRY_DELAYS = (2, 8, 20)  # 各次重试前的等待秒数


def _retryable(e: Exception) -> bool:
    """429 限流、5xx 服务端错误、网络/超时错误值得重试；其余 4xx 重试也没用。"""
    if isinstance(e, APIConnectionError):  # 含 APITimeoutError
        return True
    if isinstance(e, APIStatusError):
        return e.status_code == 429 or e.status_code >= 500
    return False


class _Progress:
    """流式调用的实时状态行：后台线程每秒刷新一次。

    显示已运行时长、已收到的思考/产出字数、工具调用个数，以及
    "距上次收到数据 N 秒"——这个值一直很小 = 正在正常生成；
    持续变大（超过 STALL_WARN_S 加警告）= 连接疑似卡住。
    """

    STALL_WARN_S = 30

    def __init__(self, role: str, model: str):
        self.role, self.model = role, model
        self.start = self.last_data = time.time()
        self.thinking = self.content = self.tool_calls = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._tick, daemon=True)

    def got_data(self, thinking: int = 0, content: int = 0, tool_call: bool = False) -> None:
        self.last_data = time.time()
        self.thinking += thinking
        self.content += content
        if tool_call:
            self.tool_calls += 1

    def _line(self) -> str:
        now = time.time()
        parts = [f"[{self.role}] {self.model} 运行中 {int(now - self.start)}s",
                 f"思考 {self.thinking} 字", f"产出 {self.content} 字"]
        if self.tool_calls:
            parts.append(f"工具调用 {self.tool_calls} 个")
        ago = int(now - self.last_data)
        parts.append(f"最近数据 {ago}s 前")
        if ago >= self.STALL_WARN_S:
            parts.append("⚠️ 长时间无数据，疑似卡住")
        return " | ".join(parts)

    def _tick(self) -> None:
        while not self._stop.wait(1.0):
            log("\r" + self._line() + " " * 8, end="", flush=True)
            heartbeat(self._snapshot("running"))

    def _snapshot(self, phase: str) -> dict:
        """结构化活性快照：写心跳文件用，内容与状态行同源。"""
        now = time.time()
        return {"role": self.role, "model": self.model, "phase": phase,
                "elapsed_s": int(now - self.start),
                "thinking_chars": self.thinking, "content_chars": self.content,
                "tool_calls": self.tool_calls,
                "last_data_ago_s": int(now - self.last_data), "ts": now}

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join()
        log()  # 状态行收尾换行，避免覆盖后续输出
        heartbeat(self._snapshot("finished"))


class _StreamMessage:
    """流式碎片重组出的消息对象，形状对齐 SDK 的 ChatCompletionMessage
    （content / reasoning_content / tool_calls / model_dump），调用方无感知。"""

    def __init__(self, content: str, reasoning: str, tool_calls: list[dict]):
        self.role = "assistant"
        self.content = content or None
        self.reasoning_content = reasoning
        self.tool_calls = [
            type("ToolCall", (), {
                "id": tc["id"], "type": "function",
                "function": type("Function", (), {"name": tc["name"],
                                                  "arguments": tc["arguments"]})(),
            })() for tc in tool_calls
        ] or None

    def model_dump(self, exclude_none: bool = False) -> dict:
        d: dict = {"role": "assistant", "content": self.content}
        if self.reasoning_content:
            d["reasoning_content"] = self.reasoning_content
        if self.tool_calls:
            d["tool_calls"] = [{"id": tc.id, "type": "function",
                                "function": {"name": tc.function.name,
                                             "arguments": tc.function.arguments}}
                               for tc in self.tool_calls]
        if exclude_none:
            d = {k: v for k, v in d.items() if v is not None}
        return d


def _collect_stream(role: str, model: str, stream):
    """消费流式响应：拼接 content / reasoning_content / tool_calls 碎片，
    同时驱动状态行，最后重组出与非流式调用同形状的响应对象。"""
    content_parts, reasoning_parts = [], []
    tool_calls: dict[int, dict] = {}
    with _Progress(role, model) as prog:
        for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta.content:
                content_parts.append(delta.content)
                prog.got_data(content=len(delta.content))
            rc = getattr(delta, "reasoning_content", None)
            if rc:
                reasoning_parts.append(rc)
                prog.got_data(thinking=len(rc))
            for tc in delta.tool_calls or []:
                slot = tool_calls.setdefault(tc.index, {"id": "", "name": "", "arguments": ""})
                if tc.id:  # id 只出现在该调用的首个碎片，用它计数
                    slot["id"] += tc.id
                    prog.got_data(tool_call=True)
                if tc.function:
                    slot["name"] += tc.function.name or ""
                    slot["arguments"] += tc.function.arguments or ""
    msg = _StreamMessage("".join(content_parts), "".join(reasoning_parts),
                         [tool_calls[i] for i in sorted(tool_calls)])
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


def _create(role: str, provider: str, **kwargs):
    """chat.completions.create 的封装：流式接收 + 瞬时错误退避重试 +
    硬错误转成友好报错。

    流式是为了可视性（见 _Progress）：非流式请求中途无任何信号，
    无法区分"正在生成"和"卡死"。流式碎片在 _collect_stream 里重组，
    对调用方暴露的形状与非流式一致。
    所有 LLM 调用都走这里，节点代码不捕获异常：流程中断后检查点还在，
    修复问题（充值、改 key）用相同 --thread-id 重跑即可从失败节点继续。
    """
    model = kwargs.get("model", "?")
    for attempt in range(MAX_RETRIES + 1):
        try:
            stream = _get_client(provider).chat.completions.create(stream=True, **kwargs)
            return _collect_stream(role, model, stream)
        except Exception as e:
            if _retryable(e) and attempt < MAX_RETRIES:
                delay = RETRY_DELAYS[attempt]
                log(f"[{role}] {provider}/{model} 调用失败（{type(e).__name__}），"
                      f"{delay}s 后重试（第 {attempt + 1}/{MAX_RETRIES} 次）")
                time.sleep(delay)
                continue
            retried = f"（已重试 {MAX_RETRIES} 次）" if attempt == MAX_RETRIES and _retryable(e) else ""
            raise RuntimeError(
                f"[{role}] 调用 {provider}/{model} 失败{retried}：\n{e}\n\n"
                "排查建议：\n"
                "- 余额/配额不足（402，或错误信息含 insufficient / quota / 额度）："
                "到对应平台充值或等额度刷新；\n"
                "- API key 无效或没有权限（401/403）："
                f"检查 {config.ENV_PATH} 里对应的 key；\n"
                "- 持续 429/5xx：对方服务故障或限流，稍后再试。\n"
                "修复后用本次会话的 --thread-id（启动时终端打印的会话 id）重新运行，"
                "会从失败的节点继续，已完成的节点不会重跑、不会重复计费。"
            ) from e


def _parse(role: str, model: str, raw: str) -> ChatResult:
    """按契约解析输出。开启原生思考后模型对标签的遵守会变松
    （比如思考已写进 reasoning_content，scratchpad/result 标签只写一半），
    所以解析分四档降级，尽量不浪费输出、也不让思考内容混进下游。"""
    thinking = ""
    m = re.search(r"<scratchpad>(.*?)</scratchpad>", raw, re.S | re.I)
    if m:
        thinking = m.group(1).strip()
    # 1. 完整的 <result>...</result>
    m = re.search(r"<result>(.*?)</result>", raw, re.S | re.I)
    if m:
        return ChatResult(thinking, m.group(1).strip(), raw, model, True)
    # 2. 有 <result> 开标签但没闭合：取其后全部内容
    m = re.search(r"<result>(.*)$", raw, re.S | re.I)
    if m and m.group(1).strip():
        log(f"[{role}] ⚠️ <result> 块未闭合，已截取标签后内容")
        return ChatResult(thinking, m.group(1).strip(), raw, model, True)
    # 3. 完全没有 result 块、但有 scratchpad：剥掉 scratchpad（含未闭合的），
    #    取剩余部分，避免思考内容原样流进下游
    rest = re.sub(r"<scratchpad>.*?(</scratchpad>|$)", "", raw, flags=re.S | re.I).strip()
    rest = re.sub(r"</?result>", "", rest).strip()
    if thinking and rest:
        log(f"[{role}] ⚠️ 输出缺少 <result> 块，已剥掉 scratchpad 取剩余内容")
        return ChatResult(thinking, rest, raw, model, False)
    # 4. 啥标记都没有：回退为全文，保证流程不中断，但留下标记供排查
    log(f"[{role}] ⚠️ 输出缺少 <result> 块，已回退为全文")
    return ChatResult(thinking, raw.strip(), raw, model, False)


def _mock_raw(system: str) -> str:
    if "<!-- role: architect -->" in system:
        body = (
            "# 大纲（mock）\n\n一、背景\n二、核心论点\n三、总结\n\n"
            "<!-- RESEARCH_BRIEF\n1. 主题相关背景资料\nRESEARCH_BRIEF -->"
        )
    elif "<!-- role: researcher -->" in system:
        body = (
            "材料标题：mock 资料\n"
            "来源：https://example.com/mock\n"
            "要点：这是一条用于测试的资料要点。\n"
        )
    elif "<!-- role: writer -->" in system:
        body = "# 初稿（mock）\n\n这是初稿正文。\n"
    elif "<!-- role: reviewer -->" in system:
        # MOCK_REVIEWER=fail 时永远判不通过，用来验证 3 次循环后强制放行
        if os.getenv("MOCK_REVIEWER", "pass") == "fail":
            body = "VERDICT: FAIL\n\n意见（mock）：论证不够充分，请补充。"
        else:
            body = "VERDICT: PASS\n\n意见（mock）：内容合格。"
    elif "<!-- role: stylist -->" in system:
        body = "# 润色稿（mock）\n\n这是润色后的正文。\n"
    else:
        body = "mock reply\n"
    return f"<scratchpad>\nmock 思考过程\n</scratchpad>\n<result>\n{body}\n</result>"


def _extra_body(role: str) -> dict:
    """按角色决定开不开原生思考（见 config.THINKING_ROLES）。"""
    cfg = config.PROVIDERS[config.ROLE_MODELS[role][0]]
    if role in config.THINKING_ROLES:
        return cfg["extra_body_thinking"]
    return cfg["extra_body"]


def _merge_reasoning(r: ChatResult, reasoning: str) -> ChatResult:
    """把原生思考（reasoning_content）并进 thinking，一起进思考日志留痕。"""
    if reasoning.strip():
        r.thinking = (f"【原生思考】\n{reasoning.strip()}\n\n【scratchpad】\n" + r.thinking
                      if r.thinking else f"【原生思考】\n{reasoning.strip()}")
    return r


def chat(role: str, system: str, user: str) -> ChatResult:
    """统一的 LLM 调用入口。role 决定用哪个 provider/模型/温度。"""
    provider, model = config.ROLE_MODELS[role]
    if config.MOCK_LLM:
        return _parse(role, f"mock-{model}", _mock_raw(system))
    resp = _create(
        role, provider,
        model=model,
        temperature=config.TEMPERATURES.get(role, 0.5),
        extra_body=_extra_body(role),
        messages=[
            {"role": "system", "content": system + CONTRACT},
            {"role": "user", "content": user},
        ],
    )
    msg = resp.choices[0].message
    return _merge_reasoning(_parse(role, model, msg.content or ""),
                            getattr(msg, "reasoning_content", None) or "")


def chat_with_tools(role: str, messages: list[dict], tools: list[dict]):
    """带工具调用的对话（供 agent5 的素材库检索循环用）。

    返回 (message, model)。message 是 SDK 原始消息对象：
    有 tool_calls 表示模型想调工具，否则 content 里是最终回答。
    调用方 append 进 messages 时需保留 tool_calls 字段，但要去掉
    reasoning_content（开启原生思考时会有；各家 API 都要求不要把它回传）。
    tools 传空列表表示本轮不允许调工具（用于循环耗尽后的收尾调用）。
    """
    provider, model = config.ROLE_MODELS[role]
    kwargs = {}
    if tools:  # 部分 provider 不接受空 tools 数组，为空就不传这个参数
        kwargs["tools"] = tools
    resp = _create(
        role, provider,
        model=model,
        temperature=config.TEMPERATURES.get(role, 0.5),
        extra_body=_extra_body(role),
        messages=messages,
        **kwargs,
    )
    return resp.choices[0].message, model
