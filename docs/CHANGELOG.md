# CHANGELOG

版本号遵循 SemVer；1.0.0 之前接口可能调整。

## 0.2.0 · 2026-09-22 · AI OS v2 写作管道

- 新流程 v2：用户提交原话与材料，管道内 AI OS 生成摘要并与用户确认后，才调度定框架、研究、初稿、内容审核、润色、成稿核验；旧 v1 任务按登记的流程版本恢复。
- AI OS 调度受程序白名单约束：修改过的稿件必须重新通过原意/事实/阅读质量三维审核与成稿核验，旧 PASS 不能批准新正文；逐问题登记修订次数，两次未解决则暂停；争议可申请一次独立复核。
- 人工确认点：摘要确认、可选短样稿选择、成稿署名确认、发布授权各自独立，批准必须携带所审阅的版本号。
- 终端页面 `tui.py`：新文章、摘要与终审确认、自然语言反馈、追加预算、模型分工、发布预览与确认；`--directory` 接续任意任务目录，`--role` 只对本次启动改分工。
- 接入解析 `ai_os_connection.py`：AI OS 按额度选择 Codex → Claude Code → DeepSeek API；专业节点每次调用前同样核验额度并按回退链切换，每次接入记入任务 `routes` 并在页面显示；`WRITING_ROLE_FALLBACK=strict` 关闭回退。
- JEV 适配器与设置（`jev_adapter.py`、`service/jev_settings.py`）：受限偏好选择、旁路预测采集、类别授权与评估报告；默认关闭。
- 专业节点：素材库连续读取（`read_corpus`）、研究原文窗口补读、结构化资料条目与引文归属核验、成稿核验的论断清单与本地引用定位。
- MCP 新工具：`writing_start_v2`、`writing_update_input`、`writing_configure_jev`、`writing_configure_roles`、`writing_extend_budget`。
- 运行时状态统一到 `.runtime/`（`WRITING_RUNTIME_DIR` 可改）；测试移入 `tests/`，pytest 临时目录固定在 `.runtime/pytest`。
- 用户数据（`topic/`、`output/`）与素材库不再入库。

## 0.1.0 · 2026-09-09

- v1 流程：定框架 → 大纲确认 → 搜资料 → 初稿 → 审核 → 润色 → 成稿核验 → 人工终审 → 保存；MCP 服务与 CLI 入口；发布预览与单独确认；证据包与知识库回库。
