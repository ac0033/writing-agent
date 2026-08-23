"""pytest 全局前置：在任何测试模块 import config 之前打开 mock 开关。

既有测试文件（test_expand_refs.py 等）在收集阶段就会 import config，
而 config 在 import 时读取 MOCK_LLM / MEMORY_ENABLED 环境变量
（import 后再设就晚了）。conftest.py 是 pytest 最先加载的文件，
在这里统一设置，保证整个 pytest 进程不发真实 LLM 请求、不接记忆服务。
"""
import os

os.environ.setdefault("MOCK_LLM", "1")
os.environ.setdefault("MEMORY_ENABLED", "0")
