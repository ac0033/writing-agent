"""存档不等于已读；补读窗口保持独立，禁止跨窗口拼接引语。"""
import json
import hashlib
from pathlib import Path

from tools.research_materials import evidence_window, parse_materials, source_window_reader


def test_supplemental_window_becomes_citable_only_after_read(tmp_path):
    body = "intro " * 100 + "target evidence is exact" + " ending" * 100
    record = evidence_window({"url": "https://example.org/paper", "title": "paper"}, body, tmp_path, 40)
    result = json.dumps({"materials": [{"source_url": record["source_url"],
                         "content": "a supported point", "quote": "target evidence is exact"}], "gaps": []})
    materials, gaps = parse_materials(result, [record])
    assert not materials and gaps
    read = source_window_reader([record], tmp_path)
    output = read(record["source_url"], query="target evidence", max_chars=100)
    assert "target evidence is exact" in output
    assert "连续字符范围" in output and "未完整读取：true" in output
    materials, gaps = parse_materials(result, [record])
    assert len(materials) == 1 and not gaps
    assert len(materials[0]["evidence_windows"]) == 1


def test_read_window_rejects_unlisted_url_changed_archive_and_escape(tmp_path):
    record = evidence_window({"url": "https://example.org/paper"}, "original", tmp_path / "archive", 20)
    read = source_window_reader([record], tmp_path / "archive")
    assert "不在本次" in read("https://example.org/other")
    from pathlib import Path
    Path(record["full_text_path"]).write_text("changed", encoding="utf-8")
    assert "指纹已变化" in read(record["source_url"])
    record["full_text_path"] = str(tmp_path / "outside.txt")
    assert "路径越界" in read(record["source_url"])


def test_quote_must_fit_one_window(tmp_path):
    record = evidence_window({"url": "https://example.org/paper"}, "abcdef", tmp_path, 3)
    read = source_window_reader([record], tmp_path)
    read(record["source_url"], start=3, max_chars=3)
    result = json.dumps({"materials": [{"source_url": record["source_url"],
                         "content": "point", "quote": "abcdef"}], "gaps": []})
    materials, gaps = parse_materials(result, [record])
    assert not materials and gaps


def test_format_materials_exposes_ranges_and_keeps_windows_separate(tmp_path):
    import graph
    record = evidence_window({"url": "https://example.org/paper", "title": "paper"}, "abcdef", tmp_path, 3)
    source_window_reader([record], tmp_path)(record["source_url"], start=3, max_chars=3)
    text = graph._format_materials([record])
    assert "原始窗口：[0, 3)" in text
    assert "补读连续窗口 [3, 6)" in text
    assert "abcdef" not in text


def test_archive_preserves_mixed_newlines_hash_and_character_offsets(tmp_path):
    body = "首行\r\n第二行\r第三行\n目标证据\r\n结束"
    record = evidence_window({"url": "https://example.org/newlines"}, body, tmp_path, 4)
    assert Path(record["full_text_path"]).read_bytes() == body.encode("utf-8")
    assert record["full_text_sha256"] == hashlib.sha256(body.encode("utf-8")).hexdigest()
    start = body.index("第二行")
    end = body.index("结束")
    output = source_window_reader([record], tmp_path)(record["source_url"], start=start, max_chars=end-start)
    assert "指纹已变化" not in output
    assert f"[{start}, {end})" in output
    assert body[start:end] in output
    window = record["evidence_windows"][0]
    assert window["text"] == body[start:end]
    assert window["sha256"] == hashlib.sha256(body[start:end].encode("utf-8")).hexdigest()
