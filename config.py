"""全局配置：provider、模型分配、温度、循环上限、路径。"""
import os
from pathlib import Path

from dotenv import load_dotenv

# .env 在仓库外，路径由环境变量指定（默认用户主目录下的 .env），显式加载
ENV_PATH = Path(os.getenv("WRITING_ENV_PATH", str(Path.home() / ".env")))
load_dotenv(ENV_PATH)

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DASHSCOPE_API_KEY = os.getenv("DASHSCOPE_API_KEY", "")
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "")

# ---- 思考与输出预算 ----
# thinking_budget：思考链最大 token 数（官方范围 1~32768）。写作任务实测思考
#   经常冲到 3 万字，单次请求跑 8 分钟以上、被服务端断流的风险随之升高；
#   压到 1 万左右保住质量收益，同时把生成时长砍回安全区间。
# max_completion_tokens：思考链 + 正文的总输出上限。思考模式下旧的 max_tokens
#   上限只有 32768 且只算正文，官方推荐改用本参数（无 32768 限制）。
THINKING_BUDGET = 10240
MAX_COMPLETION_TOKENS = 32768

# ---- 客户端超时（秒）----
# 注意 httpx 的 read timeout 是"两次收到数据之间的最长间隔"，不是请求总时长：
# 流式生成只要持续来数据就不会触发，跑 10 分钟也没问题；真正卡住 5 分钟没数据
# 才会断（_Progress 在 30 秒无数据时就已打警告）。connect 是建连超时。
LLM_READ_TIMEOUT_S = 300
LLM_CONNECT_TIMEOUT_S = 30

# ---- provider 定义（均为 OpenAI 兼容接口）----
# extra_body：默认关闭原生思考，思考走 prompt 层的 <scratchpad> 契约
#   （跨 provider 一致、可解析、可入日志）。
# extra_body_thinking：开启原生思考的参数，只对 config.THINKING_ROLES 里的角色生效
#   （A/B 测试结果：开原生思考对 writer 成稿质量提升明显，对 architect 略有提升）。
PROVIDERS = {
    "deepseek": {
        "base_url": "https://api.deepseek.com",
        "api_key": DEEPSEEK_API_KEY,
        "extra_body": {"thinking": {"type": "disabled"}},
        "extra_body_thinking": {"thinking": {"type": "enabled"}},
    },
    "dashscope": {
        # 阿里系三类 key 与端点互不相通，用错就 401：
        #   Token Plan（sk-tp- / sk-sp-）→ token-plan.cn-beijing.maas.aliyuncs.com
        #   Coding Plan（sk-sp-，已停售）→ coding.dashscope.aliyuncs.com
        #   按量付费（sk- / sk-ws-）→ dashscope.aliyuncs.com/compatible-mode
        # sk-sp- 前缀 Token Plan 和 Coding Plan 共用，默认按 Token Plan 处理；
        # 如果你买的是 Coding Plan，在 .env 里加 DASHSCOPE_PLAN=coding 显式指定。
        "base_url": {
            "token": "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
            "coding": "https://coding.dashscope.aliyuncs.com/v1",
            "payg": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        }[
            os.getenv("DASHSCOPE_PLAN")
            or ("payg" if DASHSCOPE_API_KEY.startswith(("sk-ws-",)) or
                (DASHSCOPE_API_KEY.startswith("sk-") and not DASHSCOPE_API_KEY.startswith(("sk-sp-", "sk-tp-")))
                else "token")
        ],
        "api_key": DASHSCOPE_API_KEY,
        "extra_body": {"enable_thinking": False},
        # max_completion_tokens 放在 extra_body 里是为了把 provider 专属参数集中在
        # 这一处配置；extra_body 会原样并入请求体顶层，等价于 SDK 的同名参数
        "extra_body_thinking": {"enable_thinking": True,
                                "thinking_budget": THINKING_BUDGET,
                                "max_completion_tokens": MAX_COMPLETION_TOKENS},
    },
}

# 开启原生思考的角色：只给质量敏感、对延迟不敏感的角色开
# （实测 writer 开启后耗时约 4 倍、tokens 约 2.3 倍；architect 成本几乎不变）。
# stylist 同样开启：它要检索素材库并改写表述，属于质量敏感环节。
# researcher/reviewer 是快而便宜的工具型角色，保持关闭。
THINKING_ROLES = {"architect", "writer", "stylist"}

# ---- 每个角色用哪个 provider 的哪个模型 ----
ROLE_MODELS = {
    "architect":  ("deepseek", "deepseek-v4-pro"),    # agent1 定框架/观点校对，用最强的
    "researcher": ("deepseek", "deepseek-v4-flash"),  # agent3 整理搜索结果，快且便宜
    "writer":     ("dashscope", "qwen3.8-max"),       # agent2 写初稿
    "reviewer":   ("deepseek", "deepseek-v4-flash"),  # agent4 审核
    "stylist":    ("dashscope", "qwen3.8-max"),       # agent5 润色
}

# 各角色温度：写作要一点发散，研究/审核要稳
TEMPERATURES = {
    "architect": 0.5,
    "researcher": 0.3,
    "writer": 0.7,
    "reviewer": 0.3,
    "stylist": 0.5,
}

MAX_REVIEW_CYCLES = 3      # 超限保留不通过状态，转人工检查，不自动发布
MAX_RESEARCH_ROUNDS = 2    # agent2 向 agent3 请求补充资料的上限
# 单个节点内检索工具调用的总预算（一轮并行发多个也累计）。
# prompt 里的"检索控制在 5 次以内"只是软约束，模型未必遵守（实测跑过 8 次），
# 预算是硬约束：超出后拒绝执行并要求模型基于已有信息收尾。
MAX_TOOL_CALLS = 8
SEARCH_RESULT_SNIPPET = 800  # 每条搜索结果截断长度，控制 researcher 上下文
MAX_SEARCH_QUERIES = 8
MAX_SOURCE_READS = 8
SOURCE_TEXT_LIMIT = 16000
MAX_TOOL_ROUNDS = 5
GRAPH_RECURSION_LIMIT = 100
SOURCE_FRESH_DAYS = 30

BASE_DIR = Path(__file__).parent
PROMPTS_DIR = BASE_DIR / "prompts"
OUTPUT_DIR = BASE_DIR / "output"
TOPIC_DIR = BASE_DIR / "topic"
TOPIC_MAX_BYTES = 256_000
CORPUS_DIR = BASE_DIR / "corpus"
# llm_wiki 知识库的知识层（agent1/agent2 检索用；raw/ 是原始快照，不索引）
WIKI_DIR = Path(os.getenv("WIKI_DIR", str(BASE_DIR.parent / "llm_wiki/wiki")))
KB_ROOT = WIKI_DIR.parent
SKILL_ROLES = {
    "agent1_architect.md": ("systems-thinking",),
    "agent2_writer.md": ("systems-thinking", "cognitive-receiver", "clear-reporting"),
    "agent3_researcher.md": ("clear-reporting",),
    "agent4_reviewer.md": ("systems-thinking", "clear-reporting", "cognitive-receiver"),
    "agent5_stylist.md": ("cognitive-receiver", "clear-reporting", "human-writing"),
    "agent6_final_check.md": ("systems-thinking", "clear-reporting", "cognitive-receiver"),
}
# agent5 的风格规范：human-writing skill（安装于用户级 skills 目录）
HUMAN_WRITING_SKILL_PATH = Path(os.getenv("HUMAN_WRITING_SKILL_PATH", str(Path.home() / ".kimi-code/skills/human-writing/SKILL.md")))
CHECKPOINT_DB = BASE_DIR / ".checkpoints.sqlite"
# 会话登记表：thread_id → 主题/状态/产出路径（--list 查看，--thread-id 回访）
SESSIONS_FILE = BASE_DIR / "sessions.json"

# 博客仓库本地路径；留空则 --push 时只提示不执行
BLOG_REPO_PATH = os.getenv("BLOG_REPO_PATH") or str(BASE_DIR.parents[1] / "ac0033")
BLOG_POSTS_DIR = os.getenv("BLOG_POSTS_DIR", "articles")
BLOG_REMOTE = os.getenv("BLOG_REMOTE", "origin")
BLOG_BRANCH = os.getenv("BLOG_BRANCH", "main")

MOCK_LLM = os.getenv("MOCK_LLM", "") == "1"

# ---- agent-memory 记忆服务（MCP over HTTP，接入指南方式一）----
# 服务常驻本机回环地址（agent-memory 仓库的计划任务维护），本项目作为 MCP 客户端接入。
MEMORY_MCP_URL = os.getenv("AGENT_MEMORY_MCP_URL", "http://127.0.0.1:8765/mcp")
# 本项目的记忆作用域：检索只查这个 scope + global，不污染别的项目
MEMORY_SCOPE = os.getenv("AGENT_MEMORY_SCOPE", "repo:writing")
# mock 模式或显式关闭时不接记忆；服务不在线时运行时 fail-open（见 tools/memory.py）
MEMORY_ENABLED = os.getenv("MEMORY_ENABLED", "1") == "1" and not MOCK_LLM
MEMORY_TIMEOUT = 120  # 单次调用超时（秒）；session_end 的蒸馏走 LLM，给足余量
MEMORY_CONTEXT_TIMEOUT = 5
MEMORY_RETRY_COOLDOWN = 30
