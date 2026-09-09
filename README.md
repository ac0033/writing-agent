# 技术博客写作管道

输入你的主题、观点与个人材料，输出经你确认的本地稿件；发布 GitHub 要在保存后另行确认。

## 流程

聊天 → 金字塔提炼（每个论点附具体素材、支持关系和出处）→ 保存 topic/ 主题 Markdown → 大纲 → 大纲确认 → 按论点搜证 → 初稿 → 内容审核 → 润色 → 最终核验 → 你确认文章无误 → 保存本地 → 发布预览 → 你明确同意发布 → 推送 GitHub。

聊天 MCP 入口强制先存主题文件：`writing_topic_guide()` 读取提炼规范，外层 agent 对照真实聊天整理，`writing_prepare_topic(topic, pyramid_markdown, topic_id)` 保存，再调用 `writing_start(topic_file=返回路径, auto_approve=False)`。禁止直接传聊天原文到 `idea`。文件校验只能检查结构完整，不能证明提炼忠实或事实已核实。

主题文件保存在 `topic/日期-主题-版本标识.md`，每次提炼另存，不覆盖已有材料；任务记录保留文件路径、指纹及启动时的内容。可以先只保存主题供查看，已有写作授权才启动管道。终端也可用 `uv run python main.py --topic-file "topic/实际文件名.md"` 读取它；原 CLI 手工输入入口继续保留，不会自行读取宿主聊天。

保留五个模型角色。研究员内部规划搜索；最终核验复用 reviewer 模型，用独立调用重新对照原文，并非独立人工事实核查。模型、温度、预算只在 `config.py` 配置。

- `auto_approve=True` **仅自动通过大纲**。最终稿始终停在人工确认节点，不能自动保存成稿。
- 审核超限保留 fail，允许展示待修稿，但不赋予发布资格。
- 搜索、知识库、记忆失败可以降级；缺乏可靠论据的稿件不得自动获得发布资格。
- 人工同意保存只代表保存；发布确认是另一个操作，不存在启动时一并授权发布的快捷方式。

## 安装与配置

需要 Python 3.11+ 和 uv。

```bash
git clone https://github.com/ac0033/writing-agent.git writing
cd writing
uv sync
```

真实运行从仓库外的环境文件读取 `DEEPSEEK_API_KEY`、`DASHSCOPE_API_KEY` 与 `TAVILY_API_KEY`。环境文件默认是用户主目录下的 `.env`，也可用 `WRITING_ENV_PATH` 指定。模型分工与端点选择见 [config.py](config.py)。先运行下方的 doctor 检查配置；mock 模式用于验证流程。

知识库默认位于相邻的 `llm_wiki/wiki`，可用 `WIKI_DIR` 覆盖。博客目标应通过 `BLOG_REPO_PATH` 指向自己的本地 Git 仓库；目标分支、远程与文章目录分别由 `BLOG_BRANCH`、`BLOG_REMOTE`、`BLOG_POSTS_DIR` 配置。

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

当前代码的博客目录后备值由项目所在位置推导，并不保证该目录已存在或属于当前使用者。发布前应显式配置 `BLOG_REPO_PATH`，并检查发布预览中实际读取的 Git remote、分支和文件目标。

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

稿件保存后生成：

```text
output/YYYY-MM-DD-主题-运行标识/版本标识/
  article.md         本地确认稿
  evidence.json      来源、原文片段、论据清单、检查结果与正文指纹
  reading.md         待读来源及写作用途
  thinking.md        节点过程留痕
  publication.json   仅尝试发布后出现
```

不同运行/版本互不覆盖。人工改过的旧版本不会被静默重写。证据包和过程日志默认不提交公开仓库。

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

测试强制 mock，文件写临时目录；发布测试只使用临时目录中的本地 bare Git 远端。若机器现有 pytest 临时目录权限异常，可另指定一个新的 `--basetemp` 路径。不要指向有资料的目录，因为 pytest 会清理指定的临时目录。

真实模型的事实准确率、文章质量与总耗时尚需用你的实际选题评估；不从 mock 通过推断实际生成效果。独立读者测试未执行，证据包明确记录 reader_review=unavailable。

完整操作步骤见 [使用指令指南](docs/使用指令指南.md)，实现与验证记录见 [改进记录](docs/2026-09-07-upgrade.md)。
