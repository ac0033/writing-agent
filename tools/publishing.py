"""保存后的独立发布步骤：预览绑定稿件内容，明确确认后才操作博客仓库。"""
import hashlib
import json
import subprocess
from pathlib import Path

import config
from tools.storage import exclusive


def _git(root: Path, *args: str, check=True):
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL, timeout=120)
    if check and result.returncode:
        raise RuntimeError(f"git {args[0]} 失败：{result.stderr.strip()}")
    return result


def preview(article_path: str) -> dict:
    article = Path(article_path).resolve()
    bundle = json.loads(article.with_name("evidence.json").read_text(encoding="utf-8"))
    if bundle.get("mock"):
        raise ValueError("mock 测试稿不能发布")
    digest = hashlib.sha256(article.read_bytes()).hexdigest()
    if not bundle.get("article_confirmed") or digest != bundle.get("article_sha256"):
        raise ValueError("稿件与已确认的版本不一致，请重新审阅并保存")
    if not bundle.get("publication_ready"):
        raise ValueError("稿件仍有质量问题，修正并重新确认后才能发布")
    from tools.evidence import mechanical_issues
    text = article.read_text(encoding="utf-8")
    if mechanical_issues(text, text, bundle.get("materials", [])):
        raise ValueError("引用的核验状态已失效，请重新检查后保存")
    if not config.BLOG_REPO_PATH:
        raise ValueError("尚未配置 BLOG_REPO_PATH，请指定博客本地仓库")
    root = Path(config.BLOG_REPO_PATH).resolve()
    destination = (root / config.BLOG_POSTS_DIR / (article.parent.parent.name + ".md")).resolve()
    destination.relative_to(root)
    top = Path(_git(root, "rev-parse", "--show-toplevel").stdout.strip()).resolve()
    if top != root:
        raise ValueError("BLOG_REPO_PATH 必须指向博客仓库根目录")
    if not config.BLOG_BRANCH:
        raise ValueError("请配置 BLOG_BRANCH，发布目标分支不能靠猜测")
    remote_url = _git(root, "remote", "get-url", config.BLOG_REMOTE).stdout.strip()
    plan = {"article_path": str(article), "article_sha256": digest,
            "destination": str(destination), "repository": str(root),
            "remote": config.BLOG_REMOTE, "remote_url": remote_url, "branch": config.BLOG_BRANCH,
            "status": "awaiting_publish_confirmation"}
    plan["approval_token"] = hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()
    return plan


def publish(article_path: str, *, confirmed: bool, approval_token: str) -> dict:
    if confirmed is not True:
        raise ValueError("必须在保存本地后单独取得用户发布确认")
    plan = preview(article_path)
    if not approval_token or approval_token != plan["approval_token"]:
        raise ValueError("发布预览已变化，请重新向用户确认")
    root, target = Path(plan["repository"]), Path(plan["destination"])
    journal = Path(article_path).with_name("publication.json")
    with exclusive(root / ".git" / "writing-publish.lock"):
        if _git(root, "branch", "--show-current").stdout.strip() != plan["branch"]:
            raise ValueError("当前检出分支与发布目标不一致，请先处理博客分支")
        rel = target.relative_to(root).as_posix()
        pending = _git(root, "status", "--porcelain", "--", rel).stdout.strip()
        previous = json.loads(journal.read_text(encoding="utf-8")) if journal.exists() else {}
        same_attempt = previous.get("approval_token") == approval_token
        head = _git(root, "rev-parse", "HEAD").stdout.strip()
        remote_before = _git(root, "ls-remote", plan["remote"], f"refs/heads/{plan['branch']}").stdout.split()
        if not remote_before:
            raise ValueError("远端发布分支不存在，请先完成博客仓库初始化")
        if remote_before[0] != head and not (same_attempt and previous.get("commit") == head):
            raise ValueError("博客存在其他未推送提交或远端已变化，需先同步，避免夹带发布")
        article_bytes = Path(article_path).read_bytes()
        if hashlib.sha256(article_bytes).hexdigest() != plan["article_sha256"]:
            raise ValueError("确认后稿件发生变化，请重新预览并确认")
        if pending and not (same_attempt and target.exists() and target.read_bytes() == article_bytes):
            raise ValueError("目标文章有本地未提交修改，已停止以免覆盖")
        result = {**plan, "status": "preparing"}
        def persist():
            journal.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        persist()
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(article_bytes)
            _git(root, "add", "--", rel)
            changed = _git(root, "diff", "--cached", "--quiet", "--", rel, check=False)
            if changed.returncode == 1:
                _git(root, "commit", "--only", "-m", "post: " + target.stem, "--", rel)
            elif changed.returncode != 0:
                raise RuntimeError("无法检查博客文章变更")
            result.update(status="committed", commit=_git(root, "rev-parse", "HEAD").stdout.strip())
            persist()
            _git(root, "push", plan["remote"], f"HEAD:refs/heads/{plan['branch']}")
            remote_head = _git(root, "ls-remote", plan["remote"], f"refs/heads/{plan['branch']}").stdout.split()
            if not remote_head or remote_head[0] != result["commit"]:
                raise RuntimeError("推送后的远端提交未确认，请查询后重试")
            result.update(status="pushed", deployment_status="unverified")
            persist()
            return result
        except Exception as exc:
            result.update(status="failed", error=str(exc))
            persist()
            raise
