"""log.py 的测试：stderr 日志 + 心跳文件。"""
import json

import log


def test_heartbeat_writes_when_env_set(tmp_path, monkeypatch):
    """WRITING_HEARTBEAT_FILE 非空时心跳落盘（JSON 可读）；未设置时不写不炸。"""
    f = tmp_path / "hb.json"
    monkeypatch.setenv("WRITING_HEARTBEAT_FILE", str(f))
    log.heartbeat({"role": "writer", "phase": "running", "ts": 1.0})
    assert json.loads(f.read_text(encoding="utf-8"))["phase"] == "running"

    monkeypatch.delenv("WRITING_HEARTBEAT_FILE")
    log.heartbeat({"role": "writer", "phase": "running", "ts": 2.0})  # 无操作
    assert json.loads(f.read_text(encoding="utf-8"))["ts"] == 1.0  # 内容没变


def test_log_goes_to_stderr(capsys):
    """log() 写 stderr 不碰 stdout（stdio MCP 里 stdout 是协议信道）。"""
    log.log("进度信息")
    out, err = capsys.readouterr()
    assert out == ""
    assert "进度信息" in err
