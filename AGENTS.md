# AGENTS.md

本文件给在本仓库工作的 AI agent 提供项目约定与操作指引。详细产品级说明见 `README.md`。

## 项目是什么

一个基于 LangGraph 的技术文章写作工作流：输入主题和想法，经定框架 → 搜资料 → 写初稿 → 审核 → 润色 → 成稿核验，人工确认后保存到 `output/日期-标题-会话标识/版本标识/`。多模型分工：DeepSeek（architect/researcher/reviewer）+ 千问（writer/stylist）。保存本地后，必须另行展示发布预览并取得用户明确同意，才能发布到 GitHub；自动确认大纲不包含成稿确认或发布许可。

## 常用命令

```bash
uv run python main.py                  # 跑写作流程（CLI 入口）
uv run python main.py --mock           # mock 模式：不发真实 API，调流程用
uv run python main.py --list           # 列出历史会话
uv run python main.py --thread-id xxx  # 断点续跑 / 回访已完成会话
uv run pytest                          # 跑全部测试（自动进入 mock，不发真实请求）
uv run python -m service.writing_server  # 启动 MCP 服务（cwd 必须是仓库根）
```

包管理用 `uv`（不要 pip 直装系统环境）；Python >= 3.11。

## 代码结构

- `main.py` — CLI 入口：人工确认交互、`@文件/目录` 引用展开（`expand_file_refs`）、会话登记（`sessions.json`）
- `graph.py` — LangGraph 图定义：节点、路由、interrupt、记忆同步
- `state.py` — 全局 `WritingState` 结构
- `llm.py` — 多 provider 调用封装（OpenAI 兼容接口）、`<scratchpad>/<result>` 输出契约解析、mock 实现
- `config.py` — 全局唯一配置源：provider、模型分配（`ROLE_MODELS`）、温度、循环上限、路径
- `log.py` — 日志/进度输出，**一律走 stderr**（service 以 stdio 跑 MCP，stdout 只承载 JSON-RPC）
- `tools/search.py` — Tavily 搜索封装；`tools/corpus.py` — 本地 BM25 检索（wiki 知识库 + corpus 素材库）；`tools/memory.py` — agent-memory 客户端（MCP over HTTP，fail-open）
- `service/` — 把写作流程暴露成 MCP 工具的 server 层（`writing_server.py` / `runner.py` / `snapshot.py`）
- `prompts/` — 角色 prompt 与 `skills/` 适配规范，调行为改这里
- `output/` — 每篇文章一个文件夹：`article.md`（发布稿）+ `thinking.md`（各节点思考留痕）

## 关键约定（改动时必须遵守）

0. **聊天先提炼再启动**：外层 agent 先按 `prompts/chat_to_topic.md` 将真实聊天整理成金字塔框架，逐论点附具体素材、支持关系、材料性质、对话定位，保留模型和缺口。用 `writing_prepare_topic` 保存到 `topic/` 并展示路径，再传 `topic_file` 给 `writing_start`；不得直接传原聊天到 idea，不覆盖旧主题。保存主题不代表成稿确认或发布授权。CLI 手工输入独立保留；聊天转 CLI 时必须使用 `--topic-file`，不能借手工入口绕过提炼。

1. **输出契约**：每个 LLM 节点输出 `<scratchpad>`（自由推理，进 `thinking_log`）+ `<result>`（结构化产出，给下游）。契约不依赖各家原生思考字段，统一在 content 里打标记。违反契约会触发一次修复重试。
2. **人工介入单独成节点**（`human_outline` / `human_final`），节点里只有 `interrupt()`——这样恢复执行时不会重复触发上游 LLM 调用。新增人工确认点照此模式。
3. **配置单点**：模型、温度、循环上限只改 `config.py`，不要在节点里硬编码。
4. **fail-open**：记忆服务、搜索工具失败只警告/降级，绝不中断写作主流程。
5. **测试不发真实请求**：`conftest.py` 在收集阶段设 `MOCK_LLM=1` + `MEMORY_ENABLED=0`。新测试不要绕过它；config 在 import 时读环境变量，import 后再设无效。
6. **checkpoint 语义**：`.checkpoints.sqlite` 是所有会话的断点存档，删除等于清空全部会话记忆；`sessions.json` 是会话登记表（`--list` / `--thread-id` 依赖它）。这两个文件不要随手清理。
7. **git 谨慎**：service 层成稿后会自动对仓库做 git 快照（`service/snapshot.py`）。除此之外不要主动做 git mutation。

## 环境与密钥

- API key 从仓库外的 `.env` 读取（路径由环境变量 `WRITING_ENV_PATH` 指定，默认 `~/.env`）：`DEEPSEEK_API_KEY`、`DASHSCOPE_API_KEY`、`TAVILY_API_KEY`，可选 `BLOG_REPO_PATH`（--push 用）。
- 千问端点按 key 前缀自动选择；Coding Plan 需在 .env 加 `DASHSCOPE_PLAN=coding`。
- 记忆服务默认 `http://127.0.0.1:8765/mcp`，长期记忆 scope 为 `repo:writing-topic-<topic_id>`，一个主题一个项目；工作记忆再按运行隔离。续写或改标题时传同一个 `--topic-id`。旧 `repo:writing` 记录不自动迁移；`MEMORY_ENABLED=0` 可整体关闭。
- agent5 优先读取 `~/.kimi-code/skills/human-writing/SKILL.md`，缺失时使用仓库内适配版；其他写作规范与来源指纹见 `prompts/skills/`。
- 搜索和记忆故障可以降级，但证据不足不能伪装审核通过：允许人工确认后保存本地，禁止自动发布；格式修复后仍无效的模型响应停止当前任务并保留断点。
- Git 快照只能包含本次输出；发布代码在 `tools/publishing.py`，不得绕过其二次确认、正文指纹与目标绑定检查。

## 语言与风格

代码注释、文档、prompt 用中文，风格平实具体（参考仓库现有注释的写法：解释"为什么"，不只说"是什么"）。提交信息用中文短句，如 `post: xxx`、`fix: xxx`。
