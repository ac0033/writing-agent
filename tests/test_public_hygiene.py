"""公开卫生：跟踪的文本文件不含本机路径、邮箱、密钥；私人材料目录不入库；版本号与 CHANGELOG 一致。

私人词表放在 `.notes/hygiene_private_words.txt`（一行一个，不入库），存在时一并检查。
"""
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TEXT_SUFFIXES = {".py", ".md", ".json", ".toml", ".txt", ".yml", ".yaml", ".ps1", ".cfg", ".ini", ""}
SKIP = {"uv.lock"}
# 允许出现的示例路径与占位；真实本机路径不在其中
ALLOWED = ("<仓库路径>", "<项目目录>", "D:\\work\\", "D:/work/",
           # 测试夹具里明显虚构的路径根
           "D:/project", "D:/copies/", "D:/w/", "D:/logs/", "C:/on-path/", "D:/私有目录/")
PATTERNS = {
    # 任何盘符开头的绝对路径都算，示例路径用 ALLOWED 里的占位放行
    "本机绝对路径": re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/][^\s'\"`)]+"),
    "用户主目录": re.compile(r"(?:/Users/|/home/)[A-Za-z0-9_.-]+/"),
    "邮箱": re.compile(r"[A-Za-z0-9_.+-]+@(?:gmail|outlook|qq|163|126|foxmail|hotmail)\.com"),
    "密钥": re.compile(r"\bsk-[A-Za-z0-9]{10,}\b|\bBearer\s+[A-Za-z0-9._-]{20,}"),
}


def tracked_files():
    try:
        out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("不在 git 仓库内")
    return [ROOT / p.decode("utf-8") for p in out.split(b"\0") if p]


def text_files():
    return [p for p in tracked_files() if p.suffix in TEXT_SUFFIXES and p.name not in SKIP and p.is_file()]


def private_words():
    path = ROOT / ".notes" / "hygiene_private_words.txt"
    if not path.exists():
        return []
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip() and not line.startswith("#")]


def test_tracked_text_has_no_local_paths_emails_or_keys():
    findings = []
    words = private_words()
    for path in text_files():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for index, line in enumerate(text.splitlines(), 1):
            if any(marker in line for marker in ALLOWED):
                continue
            for label, pattern in PATTERNS.items():
                if pattern.search(line):
                    findings.append(f"{path.relative_to(ROOT)}:{index} {label}")
            for word in words:
                if word in line:
                    findings.append(f"{path.relative_to(ROOT)}:{index} 私人词 {word}")
    assert not findings, "\n".join(findings)


def test_private_and_runtime_directories_are_ignored_and_untracked():
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    for entry in ("/.notes/", "/.claude/", ".runtime/", "topic/", "output/"):
        assert entry in ignore, f".gitignore 缺少 {entry}"
    tracked = {p.relative_to(ROOT).as_posix() for p in tracked_files()}
    for prefix in (".notes/", ".claude/", ".runtime/", "topic/", "output/", ".kimi-code/"):
        assert not any(p.startswith(prefix) for p in tracked), f"{prefix} 下仍有被跟踪的文件"


def test_version_matches_changelog():
    version = re.search(r'^version\s*=\s*"([^"]+)"', (ROOT / "pyproject.toml").read_text(encoding="utf-8"), re.M).group(1)
    changelog = (ROOT / "docs" / "CHANGELOG.md").read_text(encoding="utf-8")
    assert re.search(rf"^## \[?v?{re.escape(version)}\]?", changelog, re.M), f"CHANGELOG 缺少 {version} 的条目"
