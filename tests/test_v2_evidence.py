"""v2资料归属、审核维度和技能来源的必要回归。"""
import json
import pytest
import config
import graph
from tools.research_materials import parse_materials, evidence_window


def test_structured_source_quote_cannot_come_from_summary():
    records = [{"source_url": "https://example.org/a", "title": "A", "evidence_text": "原文只说样本有效"}]
    text = json.dumps({"materials": [{"source_url": "https://example.org/a", "content": "全部有效", "quote": "全部有效"}], "gaps": []}, ensure_ascii=False)
    materials, gaps = parse_materials(text, records)
    assert not materials and "无法" in gaps[0]
    text = text.replace("全部有效", "原文只说样本有效")
    materials, gaps = parse_materials(text, records)
    assert materials[0]["support_quote"] in records[0]["evidence_text"] and not gaps


def test_source_archive_preserves_tail_without_fabricated_join(tmp_path):
    body = "x" * 100 + "target evidence" + "y" * 100
    item = evidence_window({"url": "https://example.org/a", "query": "target"}, body, tmp_path, 40)
    assert "target" in item["evidence_text"] and item["truncated"]
    assert item["evidence_text"] == body[item["excerpt_start"]:item["excerpt_end"]]
    assert next(tmp_path.iterdir()).read_text() == body


def test_v2_review_requires_all_dimensions(monkeypatch):
    monkeypatch.setattr(graph, "_run", lambda *args: ("VERDICT: PASS\nINTENT: PASS\nFACTS: PASS\nREADING: FAIL", {}))
    update = graph.reviewer({"pipeline_version": "v2", "topic": "例", "outline": "纲", "draft": "稿"})
    assert update["review_verdict"] == "fail"
    assert update["review_dimensions"]["reading"] == "fail"


def test_skill_manifest_lists_actual_files_and_missing_optional(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HUMAN_WRITING_SKILL_PATH", tmp_path / "absent")
    graph._stylist_system_prompt()
    loaded = graph._prompt_files.get()
    assert any(x["path"].endswith("article-writing\\SKILL.md") or x["path"].endswith("article-writing/SKILL.md") for x in loaded)
    assert loaded[-1]["status"] == "missing_using_project_natural_expression"


def test_section_level_copy_path_applies_to_every_source_block_in_the_section():
    # 真实材料包：“保存副本：…（两个完整用户消息）”下挂两个“## 来源”小块，第二块此前丢失副本路径导致引文无法定位。
    from tools.evidence import local_quote_supported
    text = ("### 可复用能力与接入方式\n\n保存副本：D:/w/materials/conversation-shortlist.md:526-534\n用途与边界：两个完整用户消息。\n\n"
            "## datascience_workflow / claude\n来源：D:/logs/a.jsonl:1100\n\n第一条消息。\n\n"
            "## datascience_workflow / claude\n来源：D:/logs/a.jsonl:1159\n\n都同意。我认为那个skills可以作为抽象出来的能力；不能有冲突。\n\n"
            "### 模拟测试遗漏真实依赖问题\n\n保存副本：D:/w/materials/other.md:1-9\n用途与边界：x\n\n## dsflow / codex\n来源：D:/logs/b.jsonl:5\n\n另一节内容。\n")
    assert local_quote_supported(text, "D:/w/materials/conversation-shortlist.md", "我认为那个skills可以作为抽象出来的能力")
    assert local_quote_supported(text, "D:/logs/a.jsonl", "第一条消息")
    # 标点差异不算改动原文；措辞不同仍不通过；上一节的副本路径不扩大到下一节
    assert local_quote_supported(text, "D:/w/materials/conversation-shortlist.md", "抽象出来的能力，不能有冲突")
    assert not local_quote_supported(text, "D:/w/materials/conversation-shortlist.md", "抽象出来的功能")
    assert not local_quote_supported(text, "D:/w/materials/conversation-shortlist.md", "另一节内容")
    assert local_quote_supported(text, "D:/w/materials/other.md", "另一节内容")


def test_local_block_index_lists_paths_per_block():
    from tools.evidence import local_block_index
    text = "### 甲\n定位：D:/logs/a.jsonl:866\n\n来优化一版针对已知局限和bug的。\n\n### 乙\n保存副本：D:/w/t.md:112-115\n用途与边界：x\n\n原理与验收要求。\n"
    index = local_block_index(text)
    assert index[0]["source_paths"] == ["D:/logs/a.jsonl"] and index[0]["starts_with"].startswith("来优化一版")
    assert index[1]["source_paths"] == ["D:/w/t.md"] and "原理" in index[1]["starts_with"]
