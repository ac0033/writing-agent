"""A/B 测试：原生思考模式开/关对关键节点产出质量的影响。

对 architect（deepseek-v4-pro）和 writer（qwen3.8-max）两个质量敏感角色，
用完全相同的输入各跑两次：一次按现配置关闭原生思考，一次开启。
输出存到 output/thinking_ab/ 供人工对比。

用法：uv run python test_thinking_ab.py
"""
import json
import time

from openai import OpenAI

import config
import llm

OUT = config.OUTPUT_DIR / "thinking_ab"

# 各家开启原生思考的参数（关闭的参数在 config.PROVIDERS 里）
THINK_ON = {
    "deepseek": {"thinking": {"type": "enabled"}},
    "dashscope": {"enable_thinking": True},
}

# 固定输入：用项目里真实的主题文件当用户思路
USER_IDEA = (config.BASE_DIR / "topic" / "agent的演变层次.md").read_text(encoding="utf-8")
TOPIC = "AI Agent 的演变层次：从工具到协作者"

ARCHITECT_USER = f"文章主题：{TOPIC}\n\n我的想法和思路：{USER_IDEA}"

WRITER_USER = f"""文章大纲：
# {TOPIC}

一、从聊天机器人到能做事的 agent：能力跃迁的三次分层
二、当前主流 agent 处于哪一层：工具调用与规划的局限
三、通往"协作者"层还差什么：记忆、主动性与责任边界

资料库：
【资料1】Agent 能力分层讨论
来源：https://example.com/material
要点：业界常把 agent 能力分为被动应答、工具调用、自主规划、长期协作四个层次；当前多数产品处于第二到第三层之间，长期记忆和跨任务一致性是主要瓶颈。
"""


def call(role: str, thinking_on: bool, system: str, user: str) -> dict:
    if config.MOCK_LLM:
        raise RuntimeError("mock 模式不运行真实 A/B 请求")
    provider, model = config.ROLE_MODELS[role]
    cfg = config.PROVIDERS[provider]
    client = OpenAI(api_key=cfg["api_key"], base_url=cfg["base_url"])
    extra = THINK_ON[provider] if thinking_on else cfg["extra_body"]
    t0 = time.time()
    resp = client.chat.completions.create(
        model=model,
        temperature=config.TEMPERATURES[role],
        extra_body=extra,
        messages=[
            {"role": "system", "content": system + llm.CONTRACT},
            {"role": "user", "content": user},
        ],
    )
    msg = resp.choices[0].message
    return {
        "reasoning": getattr(msg, "reasoning_content", None) or "",
        "content": msg.content or "",
        "elapsed_s": round(time.time() - t0, 1),
        "usage": resp.usage.model_dump() if resp.usage else {},
    }


def run_role(role: str, prompt_file: str, user: str) -> None:
    system = (config.PROMPTS_DIR / prompt_file).read_text(encoding="utf-8")
    for on in (False, True):
        tag = "think_on" if on else "think_off"
        print(f"--- {role} / {tag} 调用中 ...")
        try:
            r = call(role, on, system, user)
        except Exception as e:
            print(f"    调用失败：{e}")
            (OUT / f"{role}_{tag}.error.txt").write_text(str(e), encoding="utf-8")
            continue
        (OUT / f"{role}_{tag}.md").write_text(
            f"<!-- meta: {json.dumps({'elapsed_s': r['elapsed_s'], 'usage': r['usage']}, ensure_ascii=False)} -->\n\n"
            f"## reasoning_content（原生思考，{len(r['reasoning'])} 字）\n\n{r['reasoning'] or '（无）'}\n\n"
            f"## content（{len(r['content'])} 字）\n\n{r['content']}\n",
            encoding="utf-8",
        )
        usage = r["usage"]
        print(f"    完成：{r['elapsed_s']}s，content {len(r['content'])} 字，"
              f"reasoning {len(r['reasoning'])} 字，tokens={usage.get('total_tokens')}")


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    run_role("architect", "agent1_architect.md", ARCHITECT_USER)
    run_role("writer", "agent2_writer.md", WRITER_USER)
    print(f"\n输出已保存到 {OUT}")
