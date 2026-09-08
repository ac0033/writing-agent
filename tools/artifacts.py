"""保存可审核的证据包；知识库导入失败不影响本地稿件。"""
import hashlib
import json
import subprocess
from pathlib import Path

import config
from log import log


def save_evidence_bundle(state: dict, directory: Path) -> str:
    body = state["final_article"]
    bundle = {"schema_version": 1, "topic": state["topic"], "topic_id": state.get("topic_id", ""),
              "thread_id": state.get("thread_id", ""), "article_sha256": hashlib.sha256(body.encode()).hexdigest(),
              "article_path": str(directory / "article.md"), "article_confirmed": True,
              "publication_ready": state.get("publication_ready", False), "mock": config.MOCK_LLM,
              "quality_issues": state.get("quality_issues", []),
              "final_check": state.get("final_check_comments", ""),
              "claims": state.get("claims", []),
              "reader_review": "unavailable", "reader_independent": False,
              "research_date": state.get("research_date", ""),
              "materials": state.get("materials", []), "sources": state.get("source_records", [])}
    path = directory / "evidence.json"
    path.write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [f"# 《{state['topic']}》待读来源", "", "原文读取不等于结论已验证；以下保留原文片段和研究用途。", ""]
    for m in state.get("materials", []):
        lines.extend([f"- [ ] [{m['title']}]({m['source_url']})", f"  - 用途与局限：{m['content']}",
                      f"  - 读取日期：{m.get('fetched_at', '未知')}；状态：{m.get('source_status', 'unverified')}"])
    reading = directory / "reading.md"
    reading.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if not config.MOCK_LLM:
        script = config.KB_ROOT / "scripts" / "import_writing.py"
        if script.is_file():
            try:
                result = subprocess.run(["uv", "run", "--no-sync", "python", str(script), str(path)],
                                        cwd=config.KB_ROOT, capture_output=True, text=True, encoding="utf-8",
                                        errors="replace", stdin=subprocess.DEVNULL, timeout=60)
                if result.returncode:
                    raise RuntimeError(result.stderr or result.stdout)
            except Exception as exc:
                log(f"[knowledge] 回库未完成，可用 evidence.json 重试：{type(exc).__name__}: {exc}")
    return str(reading)
