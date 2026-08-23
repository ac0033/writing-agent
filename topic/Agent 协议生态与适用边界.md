# 话题素材：Agent 协议生态——MCP、A2A、AGNTCY 各管哪一层，边界在哪

> 素材性质：调研笔记，供写作取用。核心论点候选：**协议选型的关键不是"哪个先进"，而是先分清你的问题是 agent-to-tool 还是 agent-to-agent、是单进程还是跨组织**。

## 一句话总览

2026 年的 agent 互操作格局已经分层清晰：**MCP 管 agent 接工具/数据，A2A 管 agent 接 agent，AGNTCY 在底下管发现与身份**。三者的旗舰标准都归 Linux Foundation 治理——治理权归属是它们能被放心采用的前提（[Gravity, 2026-07](https://gravity.fast/blog/ai-agent-interoperability-standards-2026/)）。

## 分层拆解

### MCP（Model Context Protocol）——agent-to-tool 层

- Anthropic 2024 年 11 月发布。本质是把 LSP（语言服务器协议）的思路搬到 LLM 上：给工具和模型之间一个标准接口（[FuturePicker, 2026-07](https://futurepicker.com/en/agent-interoperability-mcp-a2a-agntcy-2026-en/)）。
- 解决的是"我的 agent 怎么查数据库、读文件、调 API"，**不解决 agent 之间的对话**（[Crystl, 2026-07](https://crystl.dev/blog/agent-communication-protocols/)）。
- 边界：只适合 agent-to-tool。两个 agent 要协作时，MCP 帮不上忙。

### A2A（Agent2Agent）——agent-to-agent 层

- Google 2025 年 4 月发起，2026 年到 v1.0，归 Linux Foundation 的 Agentic AI Foundation 管，支持组织超 150 家（AWS、微软、IBM、Salesforce、SAP、Cisco）（[Luby, 2026-06](https://blog.luby.co/a2a-protocol-how-googles-agent-to-agent-standard-is-reshaping-multi-agent-enterprise-architecture-in-2026/)）。
- 核心机制三件套：
  - **Agent Card**：agent 发布的 JSON 清单，描述能力、端点、认证方式，供发现；
  - **Task**：调用方通过 JSON-RPC 2.0 over HTTPS 提交任务，任务有完整生命周期；
  - **SSE 流式回传**：任务状态更新实时推回调用方（[ArchitectureDiagram.ai, 2026-07](https://architecturediagram.ai/blog/ai-agent-protocol-stack)）。
- 设计前提：agent 分属**不同厂商、不同框架、不同团队**，互相不知道对方内部实现。典型场景是"采购 agent 和供应商的履约 agent 谈判"这种跨组织协作（[NomadX, 2026-07](https://nomadx.ae/blog/mcp-vs-a2a-protocol-2026/)）。
- 边界：**单进程内的多 agent 协作用它纯属负资产**——买的互操作性用不上，照交服务化、序列化、运维的税。另外采用数据比发布会数字复杂，落地集中在金融、供应链、IT 运维等跨组织场景（[AgentNDX, 2026-06](https://agentndx.ai/blog/a2a-protocol-adoption-mid-2026/)）。

### AGNTCY——发现与身份基础设施

- Cisco 主导的底层设施，管 agent 的发现（discovery）和身份（identity），是 MCP/A2A 下面的地基（[Gravity, 2026-07](https://gravity.fast/blog/ai-agent-interoperability-standards-2026/)）。

### 两个容易混淆的名字

- **ACP**：这个缩写被两个项目用过，其中一个已经不存在了——引用时务必指明是哪个（[Crystl, 2026-07](https://crystl.dev/blog/agent-communication-protocols/)）。
- **WebMCP**：把工具调用扩展到浏览器场景的衍生标准，和 MCP 主线不是一回事。

## 适用场景的判断框架

选型按两个轴判断：

| 问题类型 | 单进程/单团队 | 跨进程/跨组织 |
|---|---|---|
| agent 接工具/数据 | 本地函数调用（够用） | MCP |
| agent 接 agent | 框架内共享状态（如 LangGraph StateGraph） | A2A（+ AGNTCY 做发现） |

一个务实的判断清单（[Powered Solutions, 2026-08](https://ecorpit.com/mcp-vs-a2a-enterprise-agent-protocol-decision-2026/)）：只是两个互相看不见的 coding agent 要在本机协作，这些协议今天都帮不上忙——协议解决的是跨系统互操作，不是本地编排。

## 用途举例：本写作工作流项目

用这个 5 节点写作 pipeline 对照上面的框架：

- **agent3 接 Tavily 搜索、agent5 接 corpus 素材库**——agent-to-tool，单进程，所以用本地函数调用；后来配 context7 查 LangGraph 文档，是跨进程工具共享，走的 MCP。
- **5 个 agent 之间的协作**——同进程、共享 LangGraph State（大纲、资料库、初稿在内存里直接传递），checkpoint + interrupt 提供断点续跑和人工确认。若套 A2A，每个节点要变成独立 HTTP 服务，LangGraph 的 checkpoint/interrupt 会被架空，还要在 Task 生命周期上重造人工确认——买的用不上，税照交。
- **什么情况下这个项目才需要 A2A**：① 把写作 pipeline 暴露成服务，让别人的 agent（比如"选题 agent"）派活——包一层 Agent Card + Task 接口即可；② 想接第三方只以 A2A 形式提供的远程 agent（如专业事实核查 agent）——图里加一个 A2A 客户端节点，不用整体改造。

## 可展开的文章角度（候选）

- "协议治理权归 Linux Foundation 为什么是能放心采用的前提"——vendor-neutral 治理 vs 单一厂商标准。
- "A2A 采用数字的 B 面"——150 家支持组织 vs 实际落地的有限场景，发布会叙事和工程现实的差距。
- 从本项目的反例讲"什么时候不用新协议"——工程选型里"不加什么"和"加什么"同样重要。
