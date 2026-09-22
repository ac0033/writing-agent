# JEV 接入与旁路验证

状态：已实现适配器和本地 mock 测试，完成过一次真实合成 API 冒烟验证。未向 JEV 发送作者材料，未完成个人偏好一致性验证。默认关闭，不能宣称已能代表作者。

## 协议依据

使用 TypeSafe 官方 [Quick start](https://docs.typesafe.ai/introduction/quickstart) 的 HTTP 协议：POST `https://api.typesafe.ai/v1/systemone`，Bearer 认证；请求使用 `model/state/questions`，Choice 问题使用 `type/instructions/criteria`，响应读取 `answers.decision.choice/probabilities/confidence`。已有 requests 足够，不新增 SDK。

概率与 confidence 分别保留，不把它们等同于作者认可概率。见官方 [Confidence](https://docs.typesafe.ai/confidence)。API 不生成文字理由；记录中的 evidence_ids 只是提交的作者依据，不能冒称 JEV 引用了这些依据。

## 配置

配置统一由 config.py 提供：

| 字段 | 初始值 / 作用 |
|---|---|
| JEV_MODE | off；可选 shadow / enabled |
| JEV_API_KEY | 从 TYPESAFE_API_KEY 读取，禁止日志输出 |
| JEV_ENDPOINT | 官方 `https://api.typesafe.ai/v1/systemone`；目前仅支持此端点 |
| JEV_MODEL | jev-1.13.0；验证阈值对应固定模型，不自动跟随 latest |
| JEV_TIMEOUT_S | 10 秒 |
| JEV_CATEGORY_POLICIES | 空字典，默认无任何类别获准 |

类别固定为 opening、narrative_order、tone、equivalent_example。每类别需 shadow_allowed=true 才允许发送旁路任务；实际代决另需 enabled、evaluation_passed、user_approved 均为 true，以及经过该类别评估确定的 min_probability 和 min_confidence。适配器不自行写入这些设置。

## 调用契约

`evaluate_decision(request: dict) -> dict` 为同步函数。宿主程序提供：

- decision_id、summary_revision、article_revision：标明当前任务与稿件；执行结果前再次核对当前状态，避免过期结果生效。
- category、question、shared_summary：偏好类型、具体问题、当前确认摘要。
- preferences：带 id、text、source=user 的作者依据；模型代决不可作为作者依据。
- candidates：至少两个，包含 id、text、eligible=true；候选均须事先满足原意和事实约束。程序无法仅凭文字证明语义合格，资格检查仍需审核与 AI OS 完成。
- user_reserved=false、explicit_user_choice=false：有作者专属决定或已经明确指定的选择，不交 JEV 投票。
- external_authorization：provider=typesafe、decision_id=当前任务标识、granted=true。必须来自宿主保留的具体内容外发授权，不能从 AI OS 输出直接复制。

返回包含 status、action、candidate_id、executable、origin、reason 与输入版本。真实模型回答另包含 prediction、probabilities、confidence、model、输入指纹与依据标识。不记录完整材料和错误响应正文。

- off：不发送。
- shadow：记录预测，action=none、executable=false，预测不能影响流程。
- enabled：只有全部权限和概率门槛通过才能返回 executable=true。模型可选 `__need_analysis__` 或 `__ask_user__` 放弃选择。
- 超时、响应非法、配置缺失：不代决，交回 AI OS；不替作者同意任何事项。

所有结果 is_user_preference=false。未来用户认可某次选择时，另留真实用户记录，不覆写模型来源。

## 旁路对照

`compare_shadow_records` 接受预测结果与之后的真实用户选择，按类别统计样本数、一致数、一致率、选择覆盖率、应问却代决、不必要询问。同一 decision_id 去重，缺少真实用户决定标识或选项非法的不进分母。

它只是描述性计数，不自动启用。正式评估仍需：偏好整理样例与验证样例分开；加入 AI OS 和规则基线；检查撤销情况；制定每类样本规模与可接受错误率；用户确认后配置类别。当前没有真实评估样本或合格阈值。

## 已做验证

`uv run pytest tests/test_jev_adapter.py -q`：23 passed。测试覆盖保留决定、缺少依据、类别授权、低概率、拒绝选择、旁路不执行、畸形响应、超时信息保护、标签去重与 mock 禁网。该结果只验证程序边界，不证明 JEV 判断质量。

真实合成冒烟：临时 shadow 配置，虚构罗勒浇水文章、虚构偏好与两个开头选项。官方 API 返回 jev-1.13.0，选择具体案例，概率和 confidence 均为 1.0；适配器成功解析，executable=false。永久配置未改，没有使用真实作者观点、文章或项目资料。适配器只记录选择、概率与输入指纹，不保存请求正文。这只证明接口连通与解析，不是个人偏好验收，也没有开放代决。
