"""仅在临时目录的本地 Git 远端验证发布，不联网、不触碰博客。"""
import hashlib
import json
import subprocess
from pathlib import Path

import pytest
import config
from tools import publishing


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", check=True).stdout.strip()


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    remote = tmp_path / "remote.git"
    remote.mkdir()
    git(remote, "init", "--bare")
    root = tmp_path / "blog"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.name", "test")
    git(root, "config", "user.email", "test@local")
    (root / "README.md").write_text("blog\n", encoding="utf-8")
    git(root, "add", "README.md")
    git(root, "commit", "-m", "init")
    git(root, "remote", "add", "origin", str(remote))
    git(root, "push", "-u", "origin", "main")
    article = tmp_path / "output" / "run" / "rev" / "article.md"
    article.parent.mkdir(parents=True)
    article.write_bytes("已确认的文章\n".encode())
    article.with_name("evidence.json").write_text(json.dumps({"article_confirmed": True,
        "publication_ready": True, "article_sha256": hashlib.sha256(article.read_bytes()).hexdigest()}), encoding="utf-8")
    monkeypatch.setattr(config, "BLOG_REPO_PATH", str(root))
    monkeypatch.setattr(config, "BLOG_POSTS_DIR", "articles")
    monkeypatch.setattr(config, "BLOG_REMOTE", "origin")
    monkeypatch.setattr(config, "BLOG_BRANCH", "main")
    return root, remote, article


def test_publish_only_article_preserves_unrelated_staging(prepared):
    root, remote, article = prepared
    (root / "unrelated.txt").write_text("不能夹带", encoding="utf-8")
    git(root, "add", "unrelated.txt")
    plan = publishing.preview(str(article))
    assert not (root / "articles").exists()
    result = publishing.publish(str(article), confirmed=True, approval_token=plan["approval_token"])
    assert result["status"] == "pushed"
    assert git(remote, "rev-parse", "main") == result["commit"]
    assert git(root, "diff", "--cached", "--name-only") == "unrelated.txt"
    assert git(root, "show", "--pretty=", "--name-only", "HEAD") == "articles/run.md"
    again = publishing.publish(str(article), confirmed=True, approval_token=plan["approval_token"])
    assert again["commit"] == result["commit"]


def test_failed_push_can_retry_without_duplicate_commit(prepared, monkeypatch):
    root, remote, article = prepared
    plan = publishing.preview(str(article))
    real = publishing._git
    def fail_push(root, *args, **kwargs):
        if args[0] == "push":
            raise RuntimeError("模拟远端不可用")
        return real(root, *args, **kwargs)
    monkeypatch.setattr(publishing, "_git", fail_push)
    with pytest.raises(RuntimeError, match="远端不可用"):
        publishing.publish(str(article), confirmed=True, approval_token=plan["approval_token"])
    committed = git(root, "rev-parse", "HEAD")
    monkeypatch.setattr(publishing, "_git", real)
    result = publishing.publish(str(article), confirmed=True, approval_token=plan["approval_token"])
    assert result["commit"] == committed
    assert git(remote, "rev-parse", "main") == committed


def test_publish_rejects_other_outgoing_commits(prepared):
    root, remote, article = prepared
    (root / "other.md").write_text("other", encoding="utf-8")
    git(root, "add", "other.md")
    git(root, "commit", "-m", "other change")
    plan = publishing.preview(str(article))
    with pytest.raises(ValueError, match="未推送提交"):
        publishing.publish(str(article), confirmed=True, approval_token=plan["approval_token"])
    assert not (root / "articles").exists()
