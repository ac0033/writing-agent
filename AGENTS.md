# AGENTS.md

本文件给在本仓库工作的 AI agent 提供项目约定与操作指引。产品级说明见 `README.md`，页面操作见 `docs/TUI使用说明.md`。

## 项目是什么

一个基于 LangGraph 的技术文章写作工作流。当前默认流程（v2）：用户提交原话与材料 → 管道内 AI OS 生成摘要并与用户确认 → AI OS 调度定框架、研究、初稿、内容审核、润色、成稿核验六个专业节点 → 人工终审 → 保存本地 → 单独确认后发布。旧流程（v1：定框架 → 大纲确认 → 搜资料 → 初稿 → 审核 → 润色 → 核验 → 终审）按任务登记的流程版本恢复，不迁移旧检查点。

交互入口有三个：终端页面 `tui.py`、命令行 `main.py`、MCP 服务 `service/writing_server.py`。三者共用同一套图、任务管理器与确认规则。

## 常用命令

```bash
uv run python tui.py                   # 终端页面（默认任务目录 .runtime/tui）
uv run python tui.py --mock            # 无网络演练
uv run python main.py                  # 命令行流程
uv run python main.py --mock           # mock 模式：不发真实 API
uv run python main.py --list           # 列出历史会话
uv run python main.py --thread-id xxx  # 断点续跑 / 回访已完成会话
uv run pytest                          # 全部测试（tests/，自动进入 mock，不发真实请求）
uv run python -m service.writing_server  # 启动 MCP 服务（cwd 必须是仓库根）
```

包管理用 `uv`（不要 pip 直装系统环境）；Python >= 3.11。

## 代码结构

- `pipeline_v2.py` — v2 图：摘要、人工确认、AI OS 调度、专业节点包装、独立复核、短样稿、JEV、保存；程序守护版本、指纹、预算与人工确认点
- `graph.py` — v1 图与六个专业节点的实现（v2 复用这些节点函数）、工具循环、记忆同步、保存
- `state.py` — 全局 `WritingState`
- `llm.py` — 多 provider 调用封装、`<scratchpad>/<result>` 输出契约解析、mock 实现
- `agent_cli.py` — Claude Code / Codex / CodeBuddy 的非交互 CLI 适配器（独立上下文、显式权限、结果校验）
- `ai_os_connection.py` — AI OS 与专业节点的接入解析：额度门槛、回退链、任务级分工覆盖、接入记录
- `jev_adapter.py` — JEV 受限偏好选择与旁路记录；`service/jev_settings.py` — JEV 用户设置与评估账本
- `config.py` — 全局唯一配置源：provider、模型分配（`ROLE_MODELS`）、回退策略、温度、预算、路径
- `log.py` — 日志/进度输出，**一律走 stderr**（MCP 以 stdio 运行，stdout 只承载 JSON-RPC）
- `tools/` — 搜索（`search.py`）、本地 BM25 检索（`corpus.py`：wiki 知识库 + 素材库连续读取）、证据核对（`evidence.py`、`research_materials.py`）、发布（`publishing.py`）、记忆客户端（`memory.py`，fail-open）、主题文件（`topics.py`）
- `service/` — 任务管理器与 MCP 工具（`writing_server.py`）、图驱动循环（`runner.py`）、TUI 控制器（`tui_controller.py`）、预算（`model_budget.py`）、取消与进程回收（`cancellation.py`、`process_job.py`）、git 快照（`snapshot.py`）
- `prompts/` — 角色 prompt 与 `skills/` 规范；`prompts/skills/article-writing/SKILL.md` 是文章组织与表达规范
- `tests/` — 全部测试；`tests/conftest.py` 在收集阶段设 `MOCK_LLM=1`、`MEMORY_ENABLED=0`、`WRITING_ROLE_FALLBACK=strict`
- `.runtime/` — 本机运行时状态（`config.RUNTIME_DIR`，已 gitignore）：检查点、会话登记、任务登记簿、心跳、TUI 任务、额度文件、原文缓存、pytest 临时目录
- `.notes/` — 开发者私人材料（方案、验收记录、一次性脚本，已 gitignore）；`tests/test_public_hygiene.py` 守住私人信息不入库与版本号一致
- `output/` — 每篇文章一个文件夹：`article.md`（确认稿）、`evidence.json`、`thinking.md`；`topic/` — 用户的主题材料。二者是用户数据，不入库

## 关键约定（改动时必须遵守）

1. **输出契约**：每个 LLM 节点输出 `<scratchpad>`（简短核查摘要，进 `thinking_log`）+ `<result>`（结构化产出，给下游）。契约不依赖各家原生思考字段。违反契约触发一次修复重试；修复只修格式，不得改变决定。
2. **人工介入单独成节点**（`human_summary` / `human_sample` / `human_decision` / `human_final`；v1 为 `human_outline` / `human_final`），节点里只有 `interrupt()`，恢复执行不重复触发上游 LLM 调用。批准必须带所审阅的 `expected_summary_version` / `expected_article_version`，版本变化即拒绝。
3. **人工确认不能自动代替**：摘要确认、短样稿选择、最终署名确认、发布授权各自独立；auto_approve 只影响 v1 大纲。测试中的模拟确认不是用户确认。
4. **共享摘要是唯一原意依据**：AI OS 合并用户明确纠正并更新摘要；探索性问句不写成立场；摘要变化使旧审核失效。所有专业节点读取当前摘要。
5. **AI OS 只调度不代写**：允许动作由程序白名单给出；修改过的稿件必须重新通过 reviewer（原意/事实/阅读质量三维）再 final_check，旧 PASS 不能批准新正文；同一问题两次定向修订仍未解决则暂停，不自动放行。
6. **接入不静默切换**：AI OS 与专业节点每次调用前解析实际接入（配置分工 → 任务级覆盖 → 额度核验 → 回退链），每次解析写日志、记入任务 `routes` 并在页面显示；调用前不得预先写成某一家。分工覆盖只在节点边界生效。CLI 失败诊断不得用 `rate_limit` 字样判断额度用尽（Claude 每次正常输出都含该字样）。
7. **JEV 默认关闭**：接口连通不代表作者偏好验证通过；开启旁路或代决需类别配置、评估通过、用户启用及材料外发授权，模型置信度不产生权限。
8. **配置单点**：模型、温度、预算、路径只改 `config.py`，节点里不硬编码。
9. **fail-open**：记忆服务、搜索工具失败只警告/降级，绝不中断写作主流程；但证据不足不能伪装审核通过。
10. **测试不发真实请求**：新测试不要绕过 `tests/conftest.py`；config 在 import 时读环境变量，import 后再设无效。
11. **运行时状态统一在 `.runtime/`**：不要在仓库根再生成散落的状态文件或 `--basetemp` 目录；删掉整个目录等于清空全部会话记忆。
12. **git 谨慎**：service 层成稿后会自动对仓库做 git 快照（`service/snapshot.py`）；除此之外不要主动做 git mutation。发布走 `tools/publishing.py`，不得绕过预览、正文指纹与目标绑定检查。
13. **密钥不落盘**：页面输入的 API key 只随执行上下文传递，不进入任务 JSON、检查点、日志或产物。

## 环境与密钥

- API key 从仓库外的 `.env` 读取（路径由 `WRITING_ENV_PATH` 指定，默认 `~/.env`）：`DEEPSEEK_API_KEY`、`DASHSCOPE_API_KEY`、`TAVILY_API_KEY`，可选 `BLOG_REPO_PATH`。CLI 认证沿用启动进程的环境，`.env` 不得污染 CLI。
- 千问端点按 key 前缀自动选择；Coding Plan 需在 .env 加 `DASHSCOPE_PLAN=coding`。
- 记忆服务默认 `http://127.0.0.1:8765/mcp`，长期记忆 scope 为 `repo:writing-topic-<topic_id>`；`MEMORY_ENABLED=0` 可整体关闭。
- 润色节点优先读取 `HUMAN_WRITING_SKILL_PATH`（默认 `~/.kimi-code/skills/human-writing/SKILL.md`），缺失时使用仓库内后备规范并如实记录，不得声称加载了原技能全文。
- 知识库路径由 `WIKI_DIR` 指定；作者旧文目录由 `WRITING_AUTHOR_STYLE_DIR` 指定。

## 语言与风格

代码注释、文档、prompt 用中文，风格平实具体：解释"为什么"，不只说"是什么"；注释解释代码含义，不记录改动历史。提交信息用中文短句，如 `post: xxx`、`fix: xxx`。
