1. 在线chatbot（网页、app）
2. 调用api接入LLM到程序中（问答型）—— prompt engineering
3. 调用LLM接入tool自行搭建单个agent（可干活）——— context engineering
4. agentic workflow（多agent协作，但节点、框架固定）—— 生产界主流用法，harness engineering
5. 自主agent（agent根据输入具备自主分析推理规划的能力——自主决定执行步骤，还需要能识别失败、调整策略，而不只是在出错时停下来。）—— 当前主流coding agent属于这一层次（codex, claude code等)，harness engineering and loop engineering
6. agentic os（操作系统）—— 具备实时分析规划，预测决策的能力，可以管理、运筹、决策和提前行动