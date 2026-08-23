"""写作工作流的 MCP service 薄层。

不改动任何既有核心代码（graph.py / main.py / config.py / tools/），
只在外面包一层：runner 自动驱动图、snapshot 做 git 快照、
writing_server 把能力暴露成 FastMCP 工具（stdio）。
"""
