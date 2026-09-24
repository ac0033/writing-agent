"""一个主题一条版本线：保存只在线尾追加，同一次运行重试不重复，改标题不分叉。"""
import hashlib
import json

import pytest

import config
from tools import lineage


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TOPIC_DIR", tmp_path / "topic")
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path / "output")
    return tmp_path


def test_versions_chain_like_commits(dirs):
    first = lineage.append_version("blog9", "旧标题", "第一版\n", source={"thread_id": "t1"})
    retry = lineage.append_version("blog9", "旧标题", "第一版\n", source={"thread_id": "t1"})
    second = lineage.append_version("blog9", "改过的新标题", "第二版\n", source={"thread_id": "t2"})
    assert first == retry and first.name == "v1" and second.name == "v2"
    assert first.parent == second.parent == dirs / "output" / "blog9"
    log = json.loads((first.parent / "versions.json").read_text(encoding="utf-8"))
    assert [v["version"] for v in log["versions"]] == [1, 2] and log["versions"][1]["parent"] == 1
    assert log["versions"][1]["sha256"] == hashlib.sha256((second / "article.md").read_bytes()).hexdigest()
    assert log["title"] == "改过的新标题"


def test_topic_dir_follows_registered_id_not_title(dirs):
    registered = dirs / "topic" / "blog-one"
    registered.mkdir(parents=True)
    (registered / "topic.json").write_text(json.dumps({"topic_id": "blog1"}), encoding="utf-8")
    assert lineage.topic_dir_name("blog1", "任意标题") == "blog-one"
    hashed = "0123456789abcdef"
    assert lineage.topic_dir_name(hashed, "AI 时代 / 新主题") == "AI-时代-新主题"
    (dirs / "output" / "AI-时代-新主题").mkdir(parents=True)
    assert lineage.topic_dir_name("fedcba9876543210", "AI 时代 / 新主题") == "AI-时代-新主题-2"
