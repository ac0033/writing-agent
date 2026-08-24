"""人类可读的进度/状态输出，统一走 stderr。

stdio MCP 场景下 stdout 是 JSON-RPC 信道：进度信息 print 到 stdout 会污染
协议帧（dsh 侧表现为 MCP 请求超时）。本模块的 log() 默认写 stderr，
CLI 下观感与 print 完全一致。

WRITING_HEARTBEAT_FILE 环境变量非空时，heartbeat() 把结构化活性数据
原子写到该文件，供 service 层的 writing_status 暴露给外部观测者。
"""
import json
import os
import sys
from pathlib import Path


def log(*args, **kwargs) -> None:
    """print 的 stderr 版本，签名与 print 一致。"""
    kwargs.setdefault("file", sys.stderr)
    print(*args, **kwargs)


def heartbeat(payload: dict) -> None:
    """写一份心跳快照（原子替换）。观测信息，写失败不影响主流程。"""
    path = os.getenv("WRITING_HEARTBEAT_FILE", "")
    if not path:
        return
    try:
        p = Path(path)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)
    except OSError:
        pass
