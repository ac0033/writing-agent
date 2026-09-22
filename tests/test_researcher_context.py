"""研究节点的本地范文工具、上下文和增量证据闭环；不发真实模型/网页请求。"""
from copy import deepcopy
import hashlib
import json
from types import SimpleNamespace

import pytest

import config
import graph
from state import WritingState, merge_materials
from tools.research_materials import evidence_window


def tool(name, **args):
    call = SimpleNamespace(id=name, function=SimpleNamespace(name=name, arguments=json.dumps(args)))
    return SimpleNamespace(tool_calls=[call], content="", model_dump=lambda **kw: {"role": "assistant", "content": "", "tool_calls": []})


def answer(value):
    content = "<scratchpad>模拟核查</scratchpad><result>" + json.dumps(value, ensure_ascii=False) + "</result>"
    return SimpleNamespace(tool_calls=[], content=content, model_dump=lambda **kw: {"role": "assistant", "content": content})


def existing_state(tmp_path):
    source = evidence_window({"url": "https://example.test/source", "title": "既有资料"},
                             "原文支持范围明确。", tmp_path / "archive", 100)
    material = {**source, "content": "已核对的支持关系", "support_quote": "原文支持范围明确"}
    return {"pipeline_version": "v2", "topic": "本地范文核验", "user_idea": "作者观点原话",
            "outline": "完整大纲：先具体问题，再解释判断，最后说明限制。",
            "research_request": "补读素材库，核验大纲的段落推进。无需外部研究统计。",
            "materials": [material], "source_records": [source]}


def deny_network(monkeypatch):
    monkeypatch.setattr(config, "MOCK_LLM", False)
    monkeypatch.setattr(graph, "search", lambda *a, **k: pytest.fail("仅本地任务不得网页查询"))
    import tools.search
    monkeypatch.setattr(tools.search, "extract_sources", lambda *a, **k: pytest.fail("空计划不得触发网页正文提取"))


def test_real_local_tool_loop_shares_outline_reads_and_preserves_prior_materials(tmp_path, monkeypatch):
    from langgraph.graph import StateGraph, START, END
    from tools import corpus
    deny_network(monkeypatch)
    author = tmp_path / "author"
    author.mkdir()
    (author / "new.md").write_text("段落推进先摆出具体问题。\n解释判断依据。\n说明限制。\n再收束全文。", encoding="utf-8")
    monkeypatch.setattr(config, "AUTHOR_STYLE_DIR", author)
    monkeypatch.setattr(config, "CORPUS_DIR", tmp_path / "empty")
    monkeypatch.setattr(config, "SOURCE_ARCHIVE_DIR", tmp_path / "archive")
    source = existing_state(tmp_path)
    old = {"source": "author/frozen.md", "content": "已冻结的连续范文原文", "sha256": "old", "arguments": {"source": "author/frozen.md"}}
    source["style_references"] = [old]
    original = deepcopy(source)
    replies = iter([tool("search_corpus", query="段落推进"),
                    tool("read_corpus", source="author/new.md", start_line=1, max_lines=2),
                    answer({"web_queries": []}),
                    tool("read_corpus", source="author/new.md", start_line=3, max_lines=2),
                    answer({"materials": [], "gaps": []})])
    calls = []
    def chat(role, messages, schemas):
        calls.append((deepcopy(messages), deepcopy(schemas)))
        return next(replies), "fake-model"
    monkeypatch.setattr(graph.llm, "chat_with_tools", chat)
    builder = StateGraph(WritingState)
    builder.add_node("research", graph.researcher)
    builder.add_edge(START, "research")
    builder.add_edge("research", END)
    result = builder.compile().invoke(source)
    assert result["materials"] == original["materials"]
    assert result["source_records"] == original["source_records"]
    assert not result["research_gaps"]
    assert len(result["style_references"]) == 3
    assert result["style_references"][0] == old
    for ref in result["style_references"][1:]:
        assert ref["sha256"] == hashlib.sha256(ref["content"].encode()).hexdigest()
        assert "不作为事实证据" in ref["content"]
    assert {ref["arguments"]["start_line"] for ref in result["style_references"][1:]} == {1, 3}
    for messages, schemas in calls:
        assert source["outline"] in messages[1]["content"]
        assert old["content"] in messages[1]["content"]
        assert {"search_corpus", "read_corpus"} <= {s["function"]["name"] for s in schemas}
    assert "read_source_window" in {s["function"]["name"] for s in calls[-1][1]}
    assert all(entry["prompt_files"] and entry["prompt_sha256"] for entry in result["thinking_log"])
    assert [entry["node"] for entry in result["thinking_log"]] == ["research_plan", "agent3"]
    assert source == original


@pytest.mark.parametrize("plan", [[], {"web_queries": []}])
def test_explicit_empty_plan_never_falls_back_to_raw_request(tmp_path, monkeypatch, plan):
    deny_network(monkeypatch)
    replies = iter([(json.dumps(plan), {}), ('{"materials":[],"gaps":[]}', {})])
    monkeypatch.setattr(graph, "_run_tool_loop", lambda *a, **kw: next(replies))
    source = existing_state(tmp_path)
    source["research_request"] = 'search_corpus(query="段落推进") 后 read_corpus'
    result = graph.researcher(source)
    assert not result["research_gaps"]
    assert not result["source_records"]
    assert merge_materials(source["materials"], result["materials"]) == source["materials"]


def test_invalid_plan_stops_without_sending_raw_local_instruction_to_web(tmp_path, monkeypatch):
    deny_network(monkeypatch)
    monkeypatch.setattr(graph, "_run_tool_loop", lambda *a, **kw: ('{"bad":true}', {}))
    with pytest.raises(ValueError, match="尚未执行网页检索"):
        graph.researcher(existing_state(tmp_path))


@pytest.mark.parametrize("quote,valid", [("原文支持范围明确", True), ("范文证明行业收益翻倍", False)])
def test_incremental_material_quote_validation_remains_strict(tmp_path, monkeypatch, quote, valid):
    deny_network(monkeypatch)
    source = existing_state(tmp_path)
    output = {"materials": [{"source_url": "https://example.test/source", "content": "新支持关系", "quote": quote}], "gaps": []}
    replies = iter([('{"web_queries":[]}', {}), (json.dumps(output), {})])
    monkeypatch.setattr(graph, "_run_tool_loop", lambda *a, **kw: next(replies))
    delta = graph.researcher(source)
    merged = merge_materials(source["materials"], delta["materials"])
    assert len(merged) == 1
    if valid:
        assert merged[0]["content"] == "新支持关系" and not delta["research_gaps"]
    else:
        assert merged == source["materials"]
        assert any("引文无法" in gap for gap in delta["research_gaps"])


def test_previous_archived_source_can_be_supplemented_without_new_web_query(tmp_path, monkeypatch):
    deny_network(monkeypatch)
    monkeypatch.setattr(config, "SOURCE_ARCHIVE_DIR", tmp_path / "archive")
    source = existing_state(tmp_path)
    full = evidence_window({"url": "https://example.test/source", "title": "既有资料"}, "开头。" + "前文。" * 40 + "尾部真实证据", tmp_path / "archive", 10)
    source["source_records"] = [full]
    source["materials"] = [{**full, "content": "旧支持关系"}]
    original = deepcopy(source)
    def run(role, node, system, user, schemas, dispatch):
        if node == "research_plan":
            return '{"web_queries":[]}', {}
        window = dispatch["read_source_window"](source="https://example.test/source", query="尾部真实证据", max_chars=40)
        assert "尾部真实证据" in window
        return json.dumps({"materials": [{"source_url": "https://example.test/source", "content": "新增支持关系", "quote": "尾部真实证据"}], "gaps": []}), {}
    monkeypatch.setattr(graph, "_run_tool_loop", run)
    delta = graph.researcher(source)
    assert len(delta["source_records"]) == 1
    assert delta["source_records"][0]["evidence_windows"]
    assert delta["materials"][0]["support_quote"] == "尾部真实证据"
    assert source == original
