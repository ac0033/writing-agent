"""提炼入口必须先落盘、读取实际内容；测试不启动真实写作。"""
from pathlib import Path
import pytest
import config
from tools import topics
from service import writing_server as server


@pytest.fixture
def brief(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TOPIC_DIR", tmp_path / "topic")
    return """## 核心命题
工具可能随模型升级折旧，应关注问题价值。
## 金字塔总览
- 核心命题
  - 工具能力变化
  - 真实问题价值
## 分层论点与具体论据
### 工具能力变化
- **论点**：补足模型缺口的工具可能被后续能力吸收。
  - **具体素材**：用户提出数周研究的 workflow 可能被下一代模型替代。
  - **支持关系**：说明工具的某些功能可能折旧，不能推出所有 workflow 无价值。
  - **材料性质**：对话主张，待外部核验。
  - **对话定位**：当前测试对话用户第一条，workflow 例子。
## 关键模型
无定量模型。
## 边界与待验证问题
缺口：需要独立核验实际产品能力。
## 对话来源
测试对话，仅用于入口回归。
"""


def test_prepare_preserves_versions_and_reads_edits(brief):
    first = topics.prepare("工具 / 折旧", brief, "tool-value")
    path = Path(first["topic_file"])
    assert path.parent == config.TOPIC_DIR and path.suffix == ".md"
    assert topics.load(str(path))["topic_sha256"] == first["topic_sha256"]
    changed = path.read_text(encoding="utf-8").replace("无定量模型。", "用户补充：公式仅作类比。")
    path.write_text(changed, encoding="utf-8")
    second = topics.prepare("工具 / 折旧", brief, "tool-value")
    assert first["topic_file"] != second["topic_file"]
    assert "用户补充" in topics.load(str(path))["idea"]


def test_missing_specific_support_rejected(brief):
    with pytest.raises(ValueError, match="支持关系"):
        topics.prepare("测试", brief.replace("**支持关系**", "**随笔**"))
    with pytest.raises(ValueError, match="章节"):
        topics.prepare("测试", "用户说写一篇博客，助手说好的")


def test_start_requires_saved_topic_and_binds_snapshot(brief, monkeypatch):
    calls = []
    monkeypatch.setattr(server.manager, "start", lambda *args, **kwargs: calls.append((args, kwargs)) or "test-task")
    with pytest.raises(ValueError, match="topic_file"):
        server.writing_start(topic="测试")
    saved = server.writing_prepare_topic("测试", brief, "test-topic")
    with pytest.raises(ValueError, match="idea"):
        server.writing_start(topic_file=saved["topic_file"], idea="绕过文件")
    with pytest.raises(ValueError, match="不一致"):
        server.writing_start(topic_file=saved["topic_file"], topic_id="another-topic")
    assert server.writing_start(topic_file=saved["topic_file"], auto_approve=False) == "test-task"
    args, kwargs = calls[0]
    assert args[1] == Path(saved["topic_file"]).read_text(encoding="utf-8")
    assert args[2] is False and args[3] == "test-topic"
    assert kwargs["topic_sha256"] == saved["topic_sha256"]


def test_outside_topic_and_unprepared_files_rejected(brief, tmp_path):
    outside = tmp_path / "outside.md"
    outside.write_text(brief, encoding="utf-8")
    with pytest.raises(ValueError, match="目录内"):
        topics.load(str(outside))
    config.TOPIC_DIR.mkdir()
    raw = config.TOPIC_DIR / "原材料.md"
    raw.write_text(brief, encoding="utf-8")
    with pytest.raises(ValueError, match="不是已整理"):
        topics.load(str(raw))
