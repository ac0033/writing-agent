"""git 快照助手：成稿落盘后把仓库变更固化成一个 commit。

用 `git -c user.name=... -c user.email=... commit` 传身份——只作用于这一次
commit，不写 repo 的 user 配置（不动用户的 git 身份设置）。
没有任何变更时不提交、不报错，返回 None。
"""
import subprocess


def git_snapshot(repo_path, message: str) -> str | None:
    """add -A + commit。有变更返回 commit hash；无变更返回 None。

    commit 失败（比如 git 不可用）抛 RuntimeError，调用方决定是否容忍
    （runner 里快照失败不影响成稿本身，只记错误）。
    """

    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args], cwd=str(repo_path),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )

    r = git("add", "-A")
    if r.returncode != 0:
        raise RuntimeError(f"git add 失败：{r.stderr.strip()[:300]}")
    # --quiet：有暂存变更退出码 1，无变更 0
    if git("diff", "--cached", "--quiet").returncode == 0:
        return None
    r = git("-c", "user.name=writing-service",
            "-c", "user.email=writing-service@local",
            "commit", "-m", message)
    if r.returncode != 0:
        raise RuntimeError(f"git commit 失败：{r.stderr.strip()[:300]}")
    return git("rev-parse", "HEAD").stdout.strip()
