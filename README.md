# 技术博客写作管道

状态：公开预览（Public Preview）。可在本机安装试用；管道保证流程约束（人工确认点、审核门、版本绑定），文章质量取决于所接入的模型。变更记录见 [CHANGELOG](docs/CHANGELOG.md)。

## 目录

- `pipeline_v2.py`、`graph.py`、`state.py` — 写作图与节点
- `tui.py`、`main.py`、`service/` — 终端页面、命令行、MCP 服务与任务管理
- `llm.py`、`agent_cli.py`、`ai_os_connection.py`、`ai_os_setup.py`、`config.py` — 模型调用、CLI 适配、接入解析、接入页的检测与模型规范化、配置
- `jev_adapter.py`、`service/jev_settings.py`、`scripts/jev_evaluate.py` — JEV 偏好选择与评估
- `prompts/` — 角色 prompt 与写作规范；`tools/` — 检索、证据核对、发布、记忆、主题版本线（`lineage.py`）
- `tests/` — 测试；`docs/` — 使用文档与 CHANGELOG；`scripts/accept_v2.py` — 真实验收脚本
- `.runtime/`（生成）— 本机运行时状态；`topic/`、`output/`、`corpus/`（生成）— 用户材料、文章版本线、素材库，不入库（结构见 [topic 与 output 目录](docs/topic目录说明.md)）

## 终端对话界面

在项目目录运行 `uv run python tui.py`。输入主题与材料后点“开始写作”，AI OS先生成摘要；明确点击“确认摘要”后才进入正式写作。修改意见直接发到对话区，成稿确认与发布确认分别处理。运行状态和已完成节点在页面更新，历史任务可继续。

启动后先进入“选择 AI OS 模型”页，分两步：先选接入方式，再从该方式的可选模型里选。页面会检测本机是否安装了 Codex 与 Claude Code，装了的显示版本并可直接用本机登录接入，没装的不可选；另外始终可以接 DeepSeek API、OpenAI 兼容 API 或 Anthropic 兼容 API（填地址与密钥后可读取模型列表）。手写的模型名会先核对再规范成标准写法。密钥输入遮蔽，只保留在当前进程。调用 Codex / Claude Code 前读取真实额度：Codex 至少剩余 15%，Claude 至少 10%，不可验证或不足则拒绝接入并提示。这里只选 AI OS 自己用的模型，各专业节点在顶栏“分工”里另设。

本机运行时状态统一在 `.runtime/`（检查点、会话登记、MCP 任务登记簿、TUI 任务、额度文件、pytest 临时目录；可用 `WRITING_RUNTIME_DIR` 改位置）。TUI 任务默认在 `.runtime/tui/`；`--directory <目录>` 可接续验收脚本或 MCP 服务（`.runtime/service/`）创建的任务。专业节点分工可在页面顶栏“分工”里按任务修改，或用 `--role 角色=提供方[:模型]` 只对本次启动修改；每次调用前核验 CLI 额度，不足时按回退链切换并在页面显示实际接入（`WRITING_ROLE_FALLBACK=strict` 关闭回退）。`uv run python tui.py --mock`可无网络演练，使用独立的 `.runtime/tui-mock` 与 `.runtime/mock/`，不会碰到真实任务和文章，模拟结果不算真实文章验收。已保存或已发布的版本可以写意见后点“退回修改”，基于这一版开修订任务，改好后接到该主题版本线的末尾。可勾选“先确认短样稿”，在完整写作前选择A/B或提修改意见。页面操作详见[TUI使用说明](docs/TUI使用说明.md)。

Claude的额度数据接入、阈值与未知状态处理详见[AI OS额度接入说明](docs/AIOS_额度接入.md)。JEV保持关闭，偏好记录不等于允许向JEV外发或开放代决；维护与评估见[JEV验收计划](docs/jev验收计划.md)。

## AI OS 流程（v2）

新建CLI任务默认使用v2，旧会话仍按原图恢复。MCP新入口为
`writing_start_v2(topic, source_text, topic_id)`：提供用户原话及本篇材料，AI OS生成摘要，
用户确认后进入研究、定框架、写作、审核和润色。旧`writing_start(topic_file=...)`保留v1兼容，
也可显式传`pipeline_version="v2"`；`writing-source-v2`原始输入文件始终使用v2。

- 摘要确认：`writing_resume(task_id, {"approved": true, "expected_summary_version": 1})`；版本必须取自刚审阅的摘要，示例中的1不可照抄。修改时传`approved:false, feedback:"原话"`。
- 运行中纠正：`writing_update_input(task_id, feedback)`，在下一安全边界更新共享摘要，旧结果重新检查。
- 中途提问：使用自然语言`feedback`；预算暂停只有显式`additional_steps`/`additional_seconds`可增加预算，失败修订机会用`additional_revisions`。
- 最终稿：`route:"feedback"`回统筹；用户确认后传`{"route":"approve","expected_summary_version":1,"expected_article_version":2}`保存，两个版本必须对应所审阅稿件。过期或缺失版本会被拒绝，发布仍单独授权。
- 原意、事实、阅读质量分别审核；润色后的稿件重新审核并核验，旧PASS不能批准新稿。
- JEV适配器已提供，但默认`off`。真实合成测试不等于已学会作者偏好；类别授权和实际评估后才可启用代决。
- 连续范文通过`search_corpus/read_corpus`读取，实际提示词和skill加载清单进入过程记录。

验收脚本：`uv run python scripts/accept_v2.py --directory topic/<主题目录>/process/<日期-验收> --source <原始材料.md> --topic "<文章标题>" --topic-id <主题标识>`。
脚本使用隔离检查点，不自动确认摘要或成稿、不发布；重写结果与旧稿分别保留。
调度行为与验收边界见[v2调度说明](docs/v2调度说明.md)及[JEV接入说明](docs/jev接入说明.md)。

以下保留原流程说明；其中固定大纲确认、三轮审核后向下执行仅适用于v1。

输入你的主题、观点与个人材料，输出经你确认的本地稿件；发布 GitHub 要在保存后另行确认。

默认模型分工：定框架使用 DeepSeek V4 Pro API；资料整理、内容审核使用 Codex；成稿核验使用另一轮独立的 Codex 调用；初稿、润色使用 Claude Code / Opus 5。生成与审核分属不同模型家族，审核与核验复用 Codex 家族。分工在 `config.py`，入口适配在 `agent_cli.py`；运行时会按额度核验并可回退，见 [AI OS 额度接入说明](docs/AIOS_额度接入.md)。

## 流程

聊天 → 金字塔提炼（每个论点附具体素材、支持关系和出处）→ 保存 topic/ 主题 Markdown → 大纲 → 大纲确认 → 按论点搜证 → 初稿 → 内容审核 → 润色 → 最终核验 → 你确认文章无误 → 保存本地 → 发布预览 → 你明确同意发布 → 推送 GitHub。

聊天 MCP 入口强制先存主题文件：`writing_topic_guide()` 读取提炼规范，外层 agent 对照真实聊天整理，`writing_prepare_topic(topic, pyramid_markdown, topic_id)` 保存，再调用 `writing_start(topic_file=返回路径, auto_approve=False)`。禁止直接传聊天原文到 `idea`。文件校验只能检查结构完整，不能证明提炼忠实或事实已核实。

主题文件保存在 `topic/<主题目录>/sources/日期-主题-版本标识.md`，每次提炼另存，不覆盖已有材料；任务记录保留文件路径、指纹及启动时的内容。可以先只保存主题供查看，已有写作授权才启动管道。终端也可用 `uv run python main.py --topic-file "topic/<主题目录>/sources/实际文件名.md"` 读取它；原 CLI 手工输入入口继续保留，不会自行读取宿主聊天。

配置六个模型角色。研究员内部规划搜索；最终核验使用独立的 final_check 角色配置，当前与 reviewer 同属Codex，以另一轮调用对照原文，并非独立模型家族或人工事实核查。模型、温度、预算只在 `config.py` 配置。

- `auto_approve=True` **仅自动通过大纲**。最终稿始终停在人工确认节点，不能自动保存成稿。
- 审核超限保留 fail，允许展示待修稿，但不赋予发布资格。
- 搜索、知识库、记忆失败可以降级；缺乏可靠论据的稿件不得自动获得发布资格。
- 人工同意保存只代表保存；发布确认是另一个操作，不存在启动时一并授权发布的快捷方式。

## 使用

```text
uv run python -m tools.doctor
uv run python main.py
uv run python main.py --mock
uv run python main.py --list
uv run python main.py --thread-id <已有会话ID>
uv run python main.py --topic-id agent-harness
uv run python -m service.writing_server
```

同一主题写系列文章或改标题时复用 `--topic-id`。未指定时按首次输入的规范化主题生成稳定标识。已有会话从 checkpoint 续跑，不删除或重建存档。CLI 的 `@文件/目录` 引用仍可使用。

当前默认博客目标：本地为仓库上两级目录下的 `ac0033`（`BLOG_REPO_PATH` 可改），远端 `ac0033/ac0033`，分支 `main`，目录 `articles/`。这是 Markdown 仓库，未假设使用 Hugo/Hexo/Jekyll。配置可用 `BLOG_REPO_PATH`、`BLOG_POSTS_DIR`、`BLOG_REMOTE`、`BLOG_BRANCH` 覆盖；预览显示实际 Git remote。

CLI 在真实稿件保存后展示发布预览，只有输入“发布”才推送。`--push` 保留为显式要求展示该预览的兼容选项，不跳过确认。mock 稿不能发布。

## MCP 操作顺序

1. `writing_topic_guide()` → 提炼聊天 → `writing_prepare_topic(...)` 保存并展示主题 → `writing_start(topic_file=返回路径, auto_approve=True)` 开始。auto_approve 仅影响大纲。
2. `writing_status(task_id)` 查看进度与待审稿。最终确认 payload 含质量问题与核验意见。
3. **用户确认文章无误、同意保存后**，`writing_resume(task_id, {"route":"approve"})` 保存本地。
4. `writing_result(task_id)` 取本地稿件、质量状态、记忆结果与下一步提示。
5. `writing_publish_preview(task_id)` 展示文章版本、目标仓库、文件、分支与 approval_token。
6. **在上一步后单独询问用户，取得明确发布同意**，才调用 `writing_publish(task_id, approval_token, confirmed=True)`。

发布只提交本篇文件，保留其他暂存内容。目标有本地修改、分支不符、存在无关未推送提交、确认后稿件变更、证据过期时停止。推送失败保留本地稿件与发布记录，重试不重复提交。`pushed` 仅表示远端提交已确认；不冒充站点部署成功。当前稿件以 Markdown 文本为主，尚无自动生成/搬运配图步骤。

## 三个 skills 的接入

运行时规范保存在 `prompts/skills/`，对应角色由 `config.SKILL_ROLES` 装配：

- systems-thinking：大纲、写作与审核的结构、主张、证据和推断边界。
- cognitive-receiver：具体到抽象的解释顺序、必要背景、统一名称。
- Clear Reporting：区分事实/转述/推断，保存论据清单，润色后复核数字、来源和限定词。

保留适合博客的条款，不把互动课堂的逐步问答强塞进自动写作。原始文件指纹在 `prompts/skills/sources.json`，适配说明在同目录 README。原有 human-writing 外部路径可选，缺失时使用仓库后备规范。事实与作者原意 > 论证与理解 > 风格。

## 证据与知识库

研究员同时检索本地 wiki 和网络，先按需求规划查询，动态信息优先近月搜索，同时查反例、边界和一手材料。每轮查询/原文读取预算在 config 中集中控制。

来源记录含 URL、读取日期、发布日期（未知则保留未知）、原文片段、片段指纹与截断标记。资料 URL 必须来自实际检索；原文读取状态是 retrieved，不表示事实已验证。最终核验生成 claims 清单，程序检查引用片段是否确实出现在已读取原文、链接是否覆盖、数字是否在润色中变化。语义判断仍依赖模型和你的终审，不能证明所有事实正确。

wiki 检索展示笔记状态与核验日期，不把草稿当已核实来源；归档页跳过。文件变化后索引自动失效重建。

稿件保存后接到该主题的版本线末尾（每个主题一条线，只追加不分叉，见 [topic 与 output 目录](docs/topic目录说明.md)）：

```text
output/<主题目录>/
  versions.json      版本线日志：版本号、上一版、时间、来源、状态、正文 SHA256
  v<N>/              第 N 版
    article.md         本地确认稿
    evidence.json      来源、原文片段、论据清单、检查结果与正文指纹
    reading.md         待读来源及写作用途
    thinking.md        节点过程留痕
    publication.json   仅尝试发布后出现
```

同一主题按 `topic_id` 归属，改标题不会另起一条线；同一次运行重试保存同一正文不会重复追加；已有版本不会被重写。证据包和过程日志默认不提交公开仓库。

真实运行保存后调用知识库 `scripts/import_writing.py`，将新来源导入 draft 笔记和待读清单，原文片段按内容指纹保存，不冒充全文快照。已有笔记保持原样。失败只警告，保留本地证据包；可在知识库目录重试：

```text
uv run python scripts/import_writing.py <evidence.json绝对路径> --dry-run
uv run python scripts/import_writing.py <evidence.json绝对路径>
```

## 记忆

已接 agent-memory 的上下文读取、工作记忆与会话收尾：

- 长期记忆：`repo:writing-topic-<topic_id>`。同主题复用，不同主题隔离。
- 运行工作记忆：主题范围后加运行标识，避免同主题多篇并发互相覆盖；checkpoint 是运行状态的权威来源。
- 保存后先把运行待办标完成，再以含完整成稿及真实保存确认的记录做主题归档；不会使用发布确认的伪造记录。
- 旧 `repo:writing` 记忆保留，不自动复制到新主题。需要迁移时先按主题归类，确认后单独迁移，避免旧内容污染新主题。
- 复核门 blocked 不绕过；pending_review 返回给用户。服务不可用时短超时并暂缓重试，成稿不受影响。

记忆用来保存你确认的观点和偏好；文献事实由知识库和证据包承担。mock 自动禁用记忆写入。

## 验证与边界

```text
uv run pytest
```

测试强制 mock，文件写临时目录（固定为 `.runtime/pytest`，每次运行前清空）；发布测试只使用临时目录中的本地 bare Git 远端。

真实模型的事实准确率、文章质量与总耗时尚需用你的实际选题评估；不从 mock 通过推断实际生成效果。独立读者测试未执行，证据包明确记录 reader_review=unavailable。

