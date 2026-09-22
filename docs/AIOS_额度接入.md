# AI OS 接入与额度来源

`ai_os_connection.py` 仅决定 AI OS 接入，不改写专业节点的模型配置。`auto` 每次调用前依次检查 Codex、Claude，最后选择 DeepSeek API。选择及拒绝原因通过 `use_connection(settings, on_route=callback)` 返回页面。显式选择 Codex 或 Claude 时，额度不合格会拒绝该次调用，不静默改变用户刚选的模型。

- Codex：所有返回窗口最低剩余额度至少 15%。恰好 15% 允许。
- Claude：五小时和七天窗口最低剩余额度至少 10%。恰好 10% 允许。
- 无法查询、格式无效、窗口已重置、缺少窗口、观测超过 120 秒，均不能证明可接入。
- 这里的百分比是订阅额度剩余比例，不是 API 钱包金额。DeepSeek API 由真实请求报告鉴权或余额错误，不伪造余额百分比。

## Codex

调用 [官方 App Server](https://developers.openai.com/codex/app-server) 的 `initialize`、`initialized`、`account/rateLimits/read`。不创建任务，不发送文章材料。优先解析 `rateLimitsByLimitId`，兼容 `rateLimits`；没有可靠模型与额度 bucket 对照时保守采用全部返回窗口的最低值。

CLI 必须已经用需要使用的 ChatGPT 账户登录。桌面 App 的登录状态不能推定为独立 CLI 子进程的登录状态。`account/read` 返回 `account=null` 时，先检查是否在受限 sandbox 中运行，再在普通本机终端执行 `codex login status`；只有本机也未登录才需要 `codex login`。API key 登录不能据此得到订阅剩余额度。

每次调用都会重新读取额度，不缓存旧观测作为固定额度。

## Claude statusline 额度桥接

Claude Code CLI 的 `claude auth` 只有 login/logout/status，没有独立的剩余额度查询命令。[官方 statusline 文档](https://code.claude.com/docs/en/statusline) 提供 `rate_limits.five_hour`、`rate_limits.seven_day`，但该信息仅在首个 API 响应后出现。不能为绕过门槛先发送一次模型请求。本项目不会读取或复制 Claude 登录 token，也不会调用未公开的 OAuth 私有接口。

若已有正在使用的 Claude Code 交互会话，可将其官方 statusline JSON 同步到一个只含额度的本地文件。下面是 Claude Code `settings.json` 中 **statusLine 字段的示例**，需与已有设置合并，不能覆盖整个设置文件；已有 statusline 时也应合并原命令。项目不会自动修改用户配置。

```json
{
  "statusLine": {
    "type": "command",
    "command": "<仓库路径>/.venv/Scripts/python.exe <仓库路径>/ai_os_connection.py --capture-claude-quota <仓库路径>/.runtime/claude-quota.json"
  }
}
```

未设置环境变量时默认读取 `.runtime/claude-quota.json`（`config.CLAUDE_QUOTA_FILE`，与上面示例一致；`.runtime/` 整体已在 .gitignore）。需要改用其他位置时，在启动 TUI 的同一终端设置：

```powershell
$env:WRITING_CLAUDE_QUOTA_FILE = '<仓库路径>/.runtime/claude-quota.json'
```

桥接只保留数值型 `used_percentage`、`resets_at` 和本机接收时间，不保存原始 JSON、密钥、会话路径、正文或账户信息。原子替换防止读到半个文件。只信任由你当前账户 Claude Code 会话写入的文件；换账户后请更换或清空旧桥接文件。不要人工填写百分比。无数据、过期数据或缺少窗口时，TUI 必须继续显示 Claude 不可接入，直到出现新鲜有效的额度数据。

## API 接入和调用隔离

`ConnectionSettings(provider='api', api_key=..., base_url=..., model=...)` 支持 OpenAI 兼容 Chat Completions API；DeepSeek 使用配置中的默认端点、模型与已有密钥，也可在页面临时输入密钥。输入密钥只保留在当前进程，退出后不保存。地址只接受 HTTPS（本机地址可 HTTP），禁止在地址中夹带用户名、密码、查询参数或片段。模型调用失败不自动转发同一材料到别家。

`use_connection` 使用 ContextVar；在线程里执行图或后台任务时，必须在该线程里进入作用域，或明确复制上下文。不可把 ConnectionSettings/ConnectionSelection 序列化进 checkpoint、日志、会话登记或文章产物；它们的 repr 隐藏 api_key，但通用 dataclass 序列化仍会读取字段。

## 验证范围

`test_ai_os_connection.py` 使用假额度和假客户端覆盖边界、未知/过期、多个窗口/多个 bucket、切换/回退、作用域隔离、API 成功/失败、密钥字段过滤、只读 App Server 消息和子进程回收。单测不生成真实文章，也不代表三家真实 LLM 调用均验收通过。

桥接命令显式按 UTF-8 读写，避免 Windows 本地编码下含中文的会话路径解码失败或状态栏乱码。

普通终端的 PATH 通常没有 `codex`（Codex Desktop 自带 CLI 不写入 PATH）。`agent_cli.command_for` 会依次使用 `CODEX_COMMAND_JSON`、PATH、`%LOCALAPPDATA%/OpenAI/Codex/bin/*/codex.exe`（最近更新的一份）。

## 专业节点也按额度解析接入

`ai_os_connection.resolve_role(role)` 在每次专业节点调用前解析实际接入：配置分工 → 任务级覆盖（`writing_configure_roles` / TUI“模型分工”，节点边界生效）→ 额度核验（同上门槛，结果 45 秒内复用）→ 回退链 `config.ROLE_FALLBACKS`（Codex↔Claude Code→DeepSeek API，API 模型 `config.ROLE_FALLBACK_API_MODEL`）。每次解析写日志 `[节点接入]`、回报页面并记入任务 `routes`。`WRITING_ROLE_FALLBACK=strict` 时只用配置/覆盖分工，不核验、不回退。切换不允许静默：由 AI OS 自行完成并在页面显示。

## Claude Code 额度以 CLI 自报为准

Claude CLI 在 `--output-format stream-json` 的每次调用输出里都带一条 `rate_limit_event`，含账户各额度窗口的使用率、重置时间和是否已被拒绝。这比 statusline 及时：statusline 只在交互会话活动时更新，管道自己消耗的额度它看不到。

现在的来源顺序：

1. 管道内每次 Claude 调用（初稿、润色、审核、AI OS 等）自动把 CLI 自报的窗口写入本地额度文件（默认 `.runtime/claude-quota.json`，只落数值）。
2. 交互会话的 statusline 桥接仍可写同一文件。
3. 文件超过 120 秒时，向 Claude CLI 发一次最小查询（默认 `claude-haiku-4-5-20251001`，无工具、无材料、不留会话，计入请求预算），用其自报数据刷新后再判 10% 门槛。`WRITING_CLAUDE_QUOTA_PROBE=0` 关闭；`WRITING_CLAUDE_QUOTA_PROBE_MODEL` 可换模型。

判定取所有已报告窗口的最小剩余；CLI 报告某窗口已拒绝请求时，该窗口按已用 100% 记录。显示示例：`Claude Code 最低窗口剩余 23%（五小时窗口已用 77%，七天窗口已用 14%）`。

界面与日志统一显示实际接入的产品名和模型：调用前只写选择顺序，额度检查后写“AI OS 已接入 Claude Code / <模型>”，完成后写 CLI 回报的实际模型。
