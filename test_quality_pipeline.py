"""从真实故障出发验证隔离、证据和确认边界；网络全部用伪响应。"""
import hashlib
import json
from datetime import date
from pathlib import Path

import pytest
import config
import graph
import llm
from tools.identity import topic_scope, run_scope
from tools.evidence import links, mechanical_issues, record
from tools import publishing


def test_memory_saves_final_text_to_topic_and_finishes_run(tmp_path, monkeypatch):
    calls = {}
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(graph.memory, "wm_sync", lambda **kw: calls.update(wm=kw))
    def end(sid, conversation, scope=None):
        calls.update(conversation=conversation, scope=scope)
        return {"status": "ok"}
    monkeypatch.setattr(graph.memory, "session_end", end)
    state = {"topic": "主题 A", "thread_id": "run1", "polished": "已确认正文", "publication_ready": True}
    result = graph.save(state)
    assert calls["scope"] == topic_scope(state)
    assert calls["wm"]["scope"] == run_scope(state)
    assert all(t["status"] == "done" for t in calls["wm"]["todos"])
    assert any("已确认正文" in m["content"] for m in calls["conversation"])
    assert Path(result["output_path"]).read_text(encoding="utf-8") == "已确认正文\n"
    article_path = Path(result["output_path"])
    bundle = json.loads(article_path.with_name("evidence.json").read_text(encoding="utf-8"))
    assert hashlib.sha256(article_path.read_bytes()).hexdigest() == bundle["article_sha256"]
    other = graph.save({**state, "thread_id": "run2", "polished": "另一篇"})
    assert other["output_path"] != result["output_path"]


def test_topic_identity_survives_rename_and_isolates_runs():
    a = {"topic": "旧标题", "topic_id": "agent-harness", "thread_id": "1"}
    b = {**a, "topic": "新标题", "thread_id": "2"}
    assert topic_scope(a) == topic_scope(b)
    assert run_scope(a) != run_scope(b)
    assert topic_scope(a) != topic_scope({"topic": "其他主题"})


def test_missing_external_skill_and_conflicting_output(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HUMAN_WRITING_SKILL_PATH", tmp_path / "missing")
    prompt = graph._stylist_system_prompt()
    assert "Clear Reporting" in prompt and "cognitive-receiver" in prompt
    monkeypatch.setattr(llm, "chat", lambda *args: llm._parse("writer", "fake", "没有标签"))
    with pytest.raises(ValueError, match="输出契约"):
        graph._run("writer", "writer", "sys", "user")


def test_final_guard_detects_missing_source_number_and_old_evidence():
    source = record({"url": "https://example.org/paper", "fetched_at": "2000-01-01"}, "原文")
    issues = mechanical_issues("结果 10 [来源](https://example.org/paper)",
                              "结果 20 [来源](https://example.org/paper) [其他](https://fake.org/x)", [source])
    assert any("数字" in x for x in issues)
    assert any("时效" in x for x in issues)
    assert any("缺少已读取原文" in x for x in issues)


def test_inline_citation_followed_by_chinese_keeps_url_boundary():
    url = "https://example.org/project/README.md"
    article = f"我的[项目]({url})把材料组织成可检查的结果。"
    source = record({"url": url}, "项目原文")
    assert links(article) == {url}
    assert mechanical_issues(article, article, [source]) == []
    assert links(article + " [未知](https://unknown.org/page)也在正文中。") == {
        url, "https://unknown.org/page"}


def test_reviewer_receives_author_and_evidence(monkeypatch):
    prompts = []
    monkeypatch.setattr(graph, "_run", lambda role, node, system, user: (prompts.append(user) or "VERDICT: PASS", {}))
    graph.reviewer({"topic": "主题", "outline": "纲", "draft": "稿", "user_idea": "作者独特经历",
                    "materials": [{"title": "论文", "source_url": "https://example.org", "content": "要点", "evidence_text": "原文标记"}]})
    assert "作者独特经历" in prompts[0] and "原文标记" in prompts[0]


def test_review_exhaustion_does_not_become_pass(monkeypatch):
    monkeypatch.setenv("MOCK_REVIEWER", "fail")
    state = {"topic": "主题", "outline": "纲", "draft": "稿", "review_cycles": config.MAX_REVIEW_CYCLES-1}
    result = graph.reviewer(state)
    assert result["review_verdict"] == "fail" and result["forced_pass"]
    assert graph.route_after_review(result) == "stylist"


def test_publish_confirmation_and_changed_article(tmp_path, monkeypatch):
    root = tmp_path / "blog"
    root.mkdir()
    article = tmp_path / "run" / "revision" / "article.md"
    article.parent.mkdir(parents=True)
    article.write_bytes("正文".encode())
    article.with_name("evidence.json").write_text(json.dumps({"article_confirmed": True, "publication_ready": True,
        "article_sha256": hashlib.sha256(article.read_bytes()).hexdigest()}), encoding="utf-8")
    monkeypatch.setattr(config, "BLOG_REPO_PATH", str(root))
    monkeypatch.setattr(config, "BLOG_BRANCH", "main")
    from types import SimpleNamespace
    monkeypatch.setattr(publishing, "_git", lambda root, *args, **kw: SimpleNamespace(
        stdout=str(root) if args[0] == "rev-parse" else "https://github.com/test/blog", returncode=0))
    plan = publishing.preview(str(article))
    assert plan["status"] == "awaiting_publish_confirmation"
    assert not Path(plan["destination"]).exists()
    with pytest.raises(ValueError, match="单独"):
        publishing.publish(str(article), confirmed=False, approval_token=plan["approval_token"])
    article.write_text("改过的正文", encoding="utf-8")
    with pytest.raises(ValueError, match="已确认的版本"):
        publishing.publish(str(article), confirmed=True, approval_token=plan["approval_token"])


def test_corpus_refresh_and_status(tmp_path, monkeypatch):
    from tools import corpus
    monkeypatch.setattr(config, "WIKI_DIR", tmp_path)
    page = tmp_path / "test.md"
    header = '---\nstatus: draft\nlast_verified: 2000-01-01\ncanonical_url: "https://example.org/one"\nevidence_sources: ["https://example.org/two"]\n---\n'
    page.write_text(header + "agent harness 测试 " * 20, encoding="utf-8")
    first = corpus._get_index("wiki")
    assert first["chunks"][0]["status"] == "draft"
    assert len(first["chunks"][0]["links"]) == 2
    page.write_text(header + "变更后的内容 " * 20, encoding="utf-8")
    assert corpus._get_index("wiki") is not first


def test_fabricated_quote_cannot_pass_final_check(monkeypatch):
    url = "https://example.org/paper"
    source = record({"url": url}, "实验仅使用一组数据，没有测量吞吐量。")
    audit = {"verdict": "pass", "issues": [], "claims": [{"claim": "吞吐量提高", "kind": "fact",
             "source_url": url, "quote": "吞吐量提高十倍", "assessment": "supported"}]}
    monkeypatch.setattr(graph, "_run", lambda *args: (json.dumps(audit), {}))
    text = f"吞吐量提高 [论文]({url})"
    result = graph.final_check({"topic": "测试", "draft": text, "polished": text,
                               "materials": [source], "review_verdict": "pass"})
    assert not result["publication_ready"]
    assert any("无法在已读取原文定位" in x for x in result["quality_issues"])


def test_heartbeat_isolated_by_task(tmp_path, monkeypatch):
    from log import heartbeat, heartbeat_task
    from llm import _Progress
    from service import writing_server
    monkeypatch.setattr(writing_server, "HEARTBEAT_FILE", tmp_path / "heartbeat.json")
    for task_id in ("task-a", "task-b"):
        token = heartbeat_task.set(task_id)
        try:
            progress = _Progress("writer", "mock")
        finally:
            heartbeat_task.reset(token)
        # 定时线程使用创建时捕获的任务身份，不依赖线程自身的上下文。
        heartbeat(progress._snapshot("running"))
    assert writing_server.TaskManager._read_heartbeat("task-a")["task_id"] == "task-a"
    assert writing_server.TaskManager._read_heartbeat("task-b")["task_id"] == "task-b"
    assert writing_server.TaskManager._read_heartbeat("missing") is None
