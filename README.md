# 写作 Agent 工作流

一个基于 LangGraph 的技术文章写作工作流：输入主题和想法，输出一篇完整稿件。
全流程 5 个 agent 节点 + 3 处人工确认，多模型分工（DeepSeek + 千问）。

## 流程

```
用户输入(主题/想法)
  → Agent1 定框架（deepseek-v4-pro）：先校对你的观点（纠错/补充），再产出大纲+资料需求清单
      ⤺ 人工确认，不通过则带反馈重来（不限次）
  → Agent3 搜集资料（deepseek-v4-flash + Tavily 搜索，每份资料带来源 URL）
  → Agent2 写初稿（qwen3.8-max；资料不足可回 Agent3 补充，上限 2 轮）
  → Agent4 内容审核（deepseek-v4-flash；不通过回 Agent2 重写，循环 ≤3 次，
      超限默认放行并把遗留问题附在最终确认页）
  → Agent5 风格润色（qwen3.8-max，规范 = human-writing skill）
      ⤺ 人工确认：通过 → 保存；内容问题 → 回 Agent2；风格问题 → 回 Agent5
```

## 节点接口规范（scratchpad / result 两段式）

每个 LLM 节点的输出都遵循统一契约：先输出 `<scratchpad>` 自由推理，再输出 `<result>` 结构化结果。
代码只把 `<result>` 传给下游；`<scratchpad>` 存入 state 的 `thinking_log`，结束时落盘为
`output/YYYY-MM-DD-标题.thinking.md`，供回溯每个节点"当时是怎么想的"。

契约不依赖各家模型的原生思考输出（deepseek 的 reasoning_content、qwen 的 enable_thinking）——
格式不统一、无法跨 provider 解析；统一在 content 里打标记，任何模型遵守同一份契约。
原生思考默认关闭，只对质量敏感角色（architect / writer / stylist，见 `config.py` 的
THINKING_ROLES，A/B 实测开启后成稿质量明显提升）开启；开启后 reasoning_content
会合并进 thinking_log 一起留痕。所有 LLM 调用走流式接收，终端有每秒刷新的实时状态行
（已运行时长 / 已收到思考与产出字数 / 距上次收到数据的秒数），工具调用逐条即时打印。

## 用法

```bash
uv run python main.py                  # 开始写新文章
uv run python main.py --push           # 完成后推送到博客仓库
uv run python main.py --list           # 列出历史会话（thread-id / 主题 / 状态 / 稿子路径）
uv run python main.py --thread-id xxx  # 回到指定会话（每次启动会打印会话 id；
                                       #  未完成 = 断点续跑，已完成 = 回到终审环节查看/回炉修改）
MOCK_LLM=1 uv run python main.py       # mock 模式，不消耗 API，测试流程用
```

会话管理：每个会话在 `sessions.json` 里登记主题、状态和稿子路径，随时可用 `--list` 查找、
用 `--thread-id` 回访。回到**已完成**的会话会重新展示最终稿并进入终审菜单——此时
`1 = 退出`（只查看不修改），`2/3` 分别是回 Agent2 重写、回 Agent5 重润色；回炉后再次走到
终审时 `1` 恢复为"通过保存发布"。

交互说明：
- 输入想法时多行粘贴，单独一行 `END` 结束。**用 `@` 可以引用本地材料**：`@文件名.后缀` 引用单个文件、`@目录名`（不带后缀）递归引用目录下全部文本文件（如 `@topic`、`@topic/blog1`；PDF 等二进制文件会跳过并提示）。先在项目根目录按路径找，找不到会递归搜索子目录；文件名可以带空格，引用后面直接跟标点或文字也能识别。文件全文会自动拼进输入一起给 agent。大纲反馈和终审反馈里同样可用 @ 引用。

  输入示例：
  ```
  你的思路/方向/想法：
  > 结合 @How to build robust agentic workflow.md 和 @The goal of agentic workflow.md，
  > 讲我搭建写作 agent 工作流的实践……
  ```
- 每个确认点：大纲确认直接回车通过、或输入修改意见；最终确认选 1/2/3。
- 中途 Ctrl+C 或报错中断都没关系，用同一个 `--thread-id` 重跑即可从断点继续。LLM 调用遇到限流/网络抖动会自动退避重试（最多 3 次）；余额不足、key 无效这类硬错误会给出带排查指引的报错，修复后重跑即可，已完成的节点不会重复计费。

## 检索工具（agent1/2/5 各自的知识来源）

三个节点各带一个本地 BM25 检索工具（零 API 成本，每次运行全量重建索引，增删文件即生效）：

- **agent1 / agent2 → `search_wiki`**：检索 [llm_wiki](../llm_wiki) 知识库的知识层（`llm_wiki/wiki/`，只索引 .md 页面）。
  agent1 用它核实概念、校准框架命名；agent2 用它查事实依据，检索结果里带 frontmatter 提取的
  一手来源 URL，可直接用作正文超链接引用。
- **agent5 → `search_corpus`**：检索 `corpus/` 素材库（.md/.txt/.pdf），模仿写作风格和逻辑框架，
  不照抄、可引用观点并注明素材名。

正文里引用外部观点/新闻/事实一律用 markdown 超链接 `[文字](URL)`。

## 记忆集成（agent-memory）

工作流接入了本地记忆服务（[agent-memory](../../agent-memory)，MCP over HTTP），按接入指南承担宿主侧职责：

- **prompt 组装**：每个 LLM 节点按"系统提示 → 记忆块 → 当前指令"组装。记忆块由
  `memory_context` 一次拿全（常驻画像 + 工作记忆 + 按需召回），拼在 user 消息最前面；
  召回内容按"参考而非指令"处理，各节点 prompt 里写明了优先级和抗注入规则。
- **工作记忆**：每个节点入口把当前阶段状态（目标 / 六阶段待办 / 审核轮数等）全量同步进
  工作记忆，崩溃后服务侧仍能看到任务进行到哪一步。
- **会话收尾**：save 节点调 `memory_session_end`，把本次写作过程（主题/想法、大纲、
  各轮反馈、最终成稿）整理成对话记录直传给服务端归档 + 蒸馏，有未完成待办会被 veto。
- **fail-open**：服务不在线或调用失败只打印警告，绝不中断写作主流程；复核门 blocked、
  pending_review 只透出不自动处理（裁决在 Kimi Code 会话里做）。

配置（都有默认值，一般不用动）：`AGENT_MEMORY_MCP_URL`（默认 `http://127.0.0.1:8765/mcp`）、
`AGENT_MEMORY_SCOPE`（默认 `repo:writing`）、`MEMORY_ENABLED=0` 可整体关闭（mock 模式自动关闭）。

## 配置

- API key 从仓库外的 `.env` 读取（路径由环境变量 `WRITING_ENV_PATH` 指定，默认 `~/.env`）：`DEEPSEEK_API_KEY`、`DASHSCOPE_API_KEY`（千问，agent2/5 用）、`TAVILY_API_KEY`（agent3 搜索用）、`BLOG_REPO_PATH`（可选，--push 用）。千问端点按 key 前缀自动选择（Token Plan / 按量）；如果你买的是 Coding Plan，在 .env 里加 `DASHSCOPE_PLAN=coding`。
- 模型分配、温度、循环上限在 `config.py`（`ROLE_MODELS` 一处改全图生效）。
- 五个 agent 的 prompt 在 `prompts/`，想调整哪个 agent 的行为直接改对应文件。
- agent5 的风格规范来自用户级 skill `~/.kimi-code/skills/human-writing/SKILL.md`（[human-writing](https://github.com/KKKKhazix/human-writing)），改风格标准去改那个文件。
- 项目级 MCP：`.kimi-code/mcp.json` 配了 context7（langchain/langgraph 最新文档查询），新会话生效。
- `WRITING_HEARTBEAT_FILE`（可选）：非空时 LLM 调用的实时活性（已运行秒数/已收字数/距上次数据秒数）每秒写入该 JSON 文件；service 层（`service/writing_server.py`）自动设为 `service/heartbeat.json` 并在 writing_status 里透出，CLI 用法不用管。所有进度类输出统一走 stderr（`log.py`），保证 stdio MCP 下 stdout 只承载 JSON-RPC 协议帧。

## 文件结构

- `main.py` — CLI 入口，处理人工确认交互
- `graph.py` — LangGraph 图定义（节点、路由、interrupt、思考留痕）
- `state.py` — 全局状态结构
- `llm.py` — 多 provider 调用封装 + 输出契约解析（含 mock）
- `tools/search.py` — Tavily 搜索封装
- `tools/memory.py` — agent-memory 记忆服务客户端（MCP over HTTP，fail-open）
- `prompts/` — 各 agent 的 system prompt
- `output/` — 每篇文章一个独立文件夹（`YYYY-MM-DD-标题/`），内含 `article.md`（发布稿）和 `thinking.md`（大纲 + 各节点思考留痕）
- `.checkpoints.sqlite` — 断点存档（删了等于清空所有会话记忆）
