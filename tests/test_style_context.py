"""连续范文读取须提供可追溯范围，不能越出配置目录。"""
from pathlib import Path

import config
from tools.corpus import read_corpus, search_corpus


def test_continuous_author_read_and_pagination(tmp_path, monkeypatch):
    author = tmp_path / "author"
    author.mkdir()
    (author / "旧文.md").write_text("\n".join(f"第{i}段" for i in range(1, 8)), encoding="utf-8")
    monkeypatch.setattr(config, "AUTHOR_STYLE_DIR", author, raising=False)
    result = read_corpus("author/旧文.md", 2, 3)
    assert "行范围：2-4 / 7" in result
    assert "下一起始行：5" in result
    assert "未完整读取：true" in result
    assert "2: 第2段\n3: 第3段\n4: 第4段" in result
    assert "1: 第1段" not in result
    assert "不作为事实证据" in result


def test_read_blocks_outside_and_absolute_paths(tmp_path, monkeypatch):
    root = tmp_path / "corpus"
    root.mkdir()
    (tmp_path / "secret.md").write_text("private", encoding="utf-8")
    monkeypatch.setattr(config, "CORPUS_DIR", root)
    assert "读取失败" in read_corpus("../secret.md")
    assert "读取失败" in read_corpus(str(tmp_path / "secret.md"))
    assert "读取失败" in read_corpus("..\\secret.md")


def test_search_returns_readable_author_source(tmp_path, monkeypatch):
    root = tmp_path / "author"
    root.mkdir()
    (root / "sample.md").write_text("可靠性决定写作如何展开。" * 20, encoding="utf-8")
    (root / "other.md").write_text("花草树木与蓝天白云。" * 20, encoding="utf-8")
    monkeypatch.setattr(config, "AUTHOR_STYLE_DIR", root, raising=False)
    monkeypatch.setattr(config, "CORPUS_DIR", tmp_path / "none")
    result = search_corpus("可靠性", top_k=1)
    assert "author/sample.md" in result
    assert "read_corpus" in result
    assert "不作为事实证据" in result


def test_complete_read_reports_complete(tmp_path, monkeypatch):
    (tmp_path / "small.txt").write_text("开头\n展开\n结尾", encoding="utf-8")
    monkeypatch.setattr(config, "CORPUS_DIR", tmp_path)
    result = read_corpus("small.txt")
    assert "行范围：1-3 / 3" in result
    assert "未完整读取：false" in result
    assert "下一起始行：无" in result
