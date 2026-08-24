"""断流续传的回归测试：注入假 client，直接驱动 llm._create 的真实重试路径。

为什么绕过 chat()/MOCK_LLM：mock 路径根本不走 _create，测不到续传逻辑
（仓库历史上吃过"mock 路径测不到真实执行路径 bug"的亏）。这里的假流
第一次吐一半就抛连接错误，验证 _create 把已收到内容作为前缀续写，
而不是把上万字丢掉整体重来。
"""
from types import SimpleNamespace

import httpx
import pytest
from openai import APIConnectionError

import llm

MESSAGES = [{"role": "user", "content": "写一篇文章"}]


def _chunk(content="", reasoning="", tool_call=None):
    delta = SimpleNamespace(content=content or None,
                            reasoning_content=reasoning or None,
                            tool_calls=[tool_call] if tool_call else None)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta)])


def _conn_error():
    return APIConnectionError(request=httpx.Request("POST", "https://fake.local/v1"))


def _stream(*chunks, error=None):
    """造一个假流：依次吐 chunks，然后抛 error（None 则正常结束）。"""
    def gen():
        yield from chunks
        if error:
            raise error
    return gen()


class _FakeCompletions:
    """按脚本依次响应 create 调用，并记录每次收到的参数供断言。"""

    def __init__(self, script):
        self.script = script
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.script[len(self.calls) - 1]


@pytest.fixture
def fake_completions(monkeypatch):
    """注入假 client + 零退避，避免测试真等 2/8/20 秒。"""
    monkeypatch.setattr(llm, "RETRY_DELAYS", (0, 0, 0))

    def install(script):
        comp = _FakeCompletions(script)
        client = SimpleNamespace(chat=SimpleNamespace(completions=comp))
        monkeypatch.setitem(llm._clients, "dashscope", client)
        return comp

    return install


def test_broken_stream_resumes_with_prefix(fake_completions):
    comp = fake_completions([
        _stream(_chunk(content="<result>\n前半截", reasoning="想了想"),
                error=_conn_error()),
        _stream(_chunk(content="后半截\n</result>")),
    ])
    resp = llm._create("writer", "dashscope", model="qwen3.8-max",
                       messages=list(MESSAGES))
    msg = resp.choices[0].message
    assert msg.content == "<result>\n前半截后半截\n</result>"
    assert msg.reasoning_content == "想了想"
    # 续写调用：原消息原样保留 + assistant 前缀（断流前已收到的内容）+ 续写指令
    resumed = comp.calls[1]["messages"]
    assert resumed[:-2] == MESSAGES
    assert resumed[-2] == {"role": "assistant", "content": "<result>\n前半截"}
    assert resumed[-1]["role"] == "user"
    assert "续写" in resumed[-1]["content"]
    assert MESSAGES == [{"role": "user", "content": "写一篇文章"}]  # 入参未被原地修改


def test_partial_tool_call_falls_back_to_full_retry(fake_completions):
    tc = SimpleNamespace(index=0, id="call_1",
                         function=SimpleNamespace(name="search_wiki",
                                                  arguments='{"q"'))
    comp = fake_completions([
        _stream(_chunk(tool_call=tc), error=_conn_error()),
        _stream(_chunk(content="<result>\n重来的完整结果</result>")),
    ])
    resp = llm._create("writer", "dashscope", model="qwen3.8-max",
                       messages=list(MESSAGES))
    assert "重来的完整结果" in resp.choices[0].message.content
    # 半截工具参数无法安全续写：第二次调用应原样重发，不加前缀
    assert comp.calls[1]["messages"] == MESSAGES


def test_resume_budget_exhausted_then_full_retry(fake_completions):
    comp = fake_completions([
        _stream(_chunk(content="甲"), error=_conn_error()),  # → 续写 1
        _stream(_chunk(content="乙"), error=_conn_error()),  # → 续写 2
        _stream(_chunk(content="丙"), error=_conn_error()),  # 续传耗尽 → 整体重试
        _stream(_chunk(content="完整结果")),                  # 整体重试成功
    ])
    resp = llm._create("writer", "dashscope", model="qwen3.8-max",
                       messages=list(MESSAGES))
    # 整体重试是全新生成：不能拼上之前断流的"甲乙丙"
    assert resp.choices[0].message.content == "完整结果"
    assert comp.calls[1]["messages"][-2]["content"] == "甲"
    assert comp.calls[2]["messages"][-2]["content"] == "甲乙"
    assert comp.calls[3]["messages"] == MESSAGES


def test_persistent_failure_raises_friendly_error(fake_completions):
    # 注意不能用 [x] * N：那会把同一个生成器对象重复 N 次，
    # 第一个抛异常后它就空了，后续调用拿到的是空流
    fake_completions([_stream(error=_conn_error())
                      for _ in range(llm.MAX_RETRIES + 1)])
    with pytest.raises(RuntimeError, match="调用 dashscope/qwen3.8-max 失败"):
        llm._create("writer", "dashscope", model="qwen3.8-max",
                    messages=list(MESSAGES))
