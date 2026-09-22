"""service/snapshot.py 的测试：git 快照助手的行为。

用临时目录里现建的 git 仓库，不碰写作仓库本身。
"""
import subprocess

from service import snapshot


def _git(repo, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(repo),
                          capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def test_no_changes_no_commit(tmp_path):
    """没有任何变更：返回 None，不产生 commit。"""
    _git(tmp_path, "init")
    assert snapshot.git_snapshot(tmp_path, "msg") is None
    assert _git(tmp_path, "rev-list", "--count", "HEAD").returncode != 0 or \
        _git(tmp_path, "rev-list", "--count", "HEAD").stdout.strip() == "0"


def test_commit_with_service_identity(tmp_path):
    """有变更：提交并返回 hash；作者身份是 writing-service，且不改 repo 配置。"""
    _git(tmp_path, "init")
    (tmp_path / "article.md").write_text("成稿", encoding="utf-8")

    commit = snapshot.git_snapshot(tmp_path, "post: 测试")
    assert commit

    log = _git(tmp_path, "log", "-1", "--format=%an|%ae|%s").stdout.strip()
    assert log == "writing-service|writing-service@local|post: 测试"
    # -c 传身份不写配置：repo 的 local 配置里 user.name/user.email 仍为空
    # （global 配置可能本来就有值，不属于本次提交写入，不查）
    assert _git(tmp_path, "config", "--local", "user.name").stdout.strip() == ""
    assert _git(tmp_path, "config", "--local", "user.email").stdout.strip() == ""

    # 再来一次无变更：不提交不报错
    assert snapshot.git_snapshot(tmp_path, "again") is None
    assert _git(tmp_path, "rev-list", "--count", "HEAD").stdout.strip() == "1"
