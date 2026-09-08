"""pytest 全局前置：在任何测试模块 import config 之前打开 mock 开关。

既有测试文件（test_expand_refs.py 等）在收集阶段就会 import config，
而 config 在 import 时读取 MOCK_LLM / MEMORY_ENABLED 环境变量
（import 后再设就晚了）。conftest.py 是 pytest 最先加载的文件，
在这里统一设置，保证整个 pytest 进程不发真实 LLM 请求、不接记忆服务。
"""
import os

os.environ["MOCK_LLM"] = "1"
os.environ["MEMORY_ENABLED"] = "0"

# 观测文件也不得污染正在运行的生产任务。
import pytest

@pytest.fixture(autouse=True)
def isolated_heartbeat(tmp_path, monkeypatch):
    monkeypatch.setenv("WRITING_HEARTBEAT_FILE", str(tmp_path / "heartbeat.json"))
