"""一个主题一条版本线：topic/<主题目录>/ 放输入材料与写作过程，output/<主题目录>/ 放成稿快照 v1、v2……

主题按 topic_id 归属，不按标题：同一主题改了标题，新稿仍接在原来那条线后面。每次保存只在线尾追加一个版本、
不分叉，像 git 提交；output/<主题目录>/versions.json 是这条线的日志（版本号、上一版、时间、来源、正文哈希）。
主题目录名在首次建立时确定，之后以目录里 topic.json / versions.json 记录的 topic_id 为准，目录改名不影响归属。
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import time

import config
from tools.storage import exclusive

REGISTRY_FILE = "topic.json"
LOG_FILE = "versions.json"


def _slug(text: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f\s]+', "-", text or "").strip("-. ")[:40] or "topic"


def _owners(path: Path) -> set[str]:
    """该目录认领的 topic_id：主 id 加 aliases（旧任务未显式给 id 时按标题哈希得到的 id）。"""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    if not isinstance(value, dict):
        return set()
    return {str(value.get("topic_id", ""))} | {str(alias) for alias in value.get("aliases", [])}


def find_topic_dir(topic_id: str) -> str:
    """已登记的主题目录名；没有返回空串。先看 topic/，再看 output/。"""
    for base, name in ((config.TOPIC_DIR, REGISTRY_FILE), (config.OUTPUT_DIR, LOG_FILE)):
        if Path(base).is_dir():
            for child in sorted(Path(base).iterdir()):
                if child.is_dir() and topic_id in _owners(child / name):
                    return child.name
    return ""


def topic_dir_name(topic_id: str, title: str) -> str:
    """主题目录名：已登记就沿用；新主题用显式 topic_id（可读），自动生成的哈希 id 改用标题，重名时加序号。"""
    found = find_topic_dir(topic_id)
    if found:
        return found
    readable = re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", topic_id or "") and not re.fullmatch(r"[0-9a-f]{16}", topic_id)
    base = topic_id if readable else _slug(title)
    name, number = base, 2
    while (Path(config.TOPIC_DIR) / name).exists() or (Path(config.OUTPUT_DIR) / name).exists():
        name, number = f"{base}-{number}", number + 1
    return name


def topic_dir(topic_id: str, title: str) -> Path:
    """topic/<主题目录>/，并确保 topic.json 记下归属。"""
    directory = Path(config.TOPIC_DIR) / topic_dir_name(topic_id, title)
    directory.mkdir(parents=True, exist_ok=True)
    registry = directory / REGISTRY_FILE
    if not registry.exists():
        registry.write_text(json.dumps({"topic_id": topic_id, "title": title}, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8")
    return directory


def read_log(directory: Path) -> dict:
    try:
        value = json.loads((Path(directory) / LOG_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_log(directory: Path, log: dict) -> None:
    target = Path(directory) / LOG_FILE
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(log, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(target)


def _locked(path: Path, wait_s: float = 10.0):
    """版本日志的锁：同一主题两次保存几乎不会并发，拿不到时短暂重试而不是直接失败。"""
    deadline = time.monotonic() + wait_s
    while True:
        lock = exclusive(path)
        try:
            lock.__enter__()
            return lock
        except RuntimeError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.2)


def append_version(topic_id: str, title: str, article: str, *, source: dict) -> Path:
    """在主题版本线尾追加一版并写入正文，返回该版本目录。

    同一次运行重复保存同一正文（节点重试）时返回已有版本，不重复追加；正文按 LF 字节落盘，哈希可复核。
    """
    directory = Path(config.OUTPUT_DIR) / topic_dir_name(topic_id, title)
    directory.mkdir(parents=True, exist_ok=True)
    data = article.encode("utf-8")
    digest = hashlib.sha256(data).hexdigest()
    lock = _locked(directory / ".versions.lock")
    try:
        log = read_log(directory) or {"topic_id": topic_id, "title": title, "versions": []}
        versions = log.setdefault("versions", [])
        head = versions[-1] if versions else None
        if head and head.get("sha256") == digest and head.get("thread_id") == source.get("thread_id"):
            return directory / f"v{head['version']}"
        number = int(head["version"]) + 1 if head else 1
        version_dir = directory / f"v{number}"
        version_dir.mkdir(parents=True, exist_ok=False)
        (version_dir / "article.md").write_bytes(data)
        versions.append({"version": number, "parent": head["version"] if head else None,
                         "created_at": datetime.now().isoformat(timespec="seconds"), "title": title,
                         "sha256": digest, "status": "已确认", **source})
        log["title"] = title
        _write_log(directory, log)
        return version_dir
    finally:
        lock.__exit__(None, None, None)
