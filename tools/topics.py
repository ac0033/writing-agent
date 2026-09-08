"""聊天 agent 先提炼，确定性入口负责校验、保存并读取同一份主题文件。"""
import hashlib
import json
import re
import uuid
from datetime import date
from pathlib import Path

import config
from tools.identity import topic_id as resolve_topic_id

SECTIONS = ("核心命题", "金字塔总览", "分层论点与具体论据", "关键模型", "边界与待验证问题", "对话来源")
FIELDS = ("具体素材", "支持关系", "材料性质", "对话定位")


def validate(markdown: str) -> None:
    if not isinstance(markdown, str) or len(markdown.encode("utf-8")) > config.TOPIC_MAX_BYTES:
        raise ValueError("主题内容必须为 Markdown 文本且不超过 TOPIC_MAX_BYTES")
    positions = []
    for section in SECTIONS:
        matches = list(re.finditer(r"^## " + section + r"\s*$", markdown, re.M))
        if len(matches) != 1:
            raise ValueError(f"主题提炼需要唯一章节：## {section}")
        positions.append(matches[0].start())
        tail = markdown[matches[0].end():]
        if not re.split(r"^## ", tail, maxsplit=1, flags=re.M)[0].strip():
            raise ValueError(f"章节不能为空：{section}；没有材料时明确写待补充")
    if positions != sorted(positions):
        raise ValueError("请按核心命题、总览、分层论点、模型、边界、来源的顺序整理")
    arguments = markdown[positions[2]:positions[3]]
    nodes = re.split(r"^- \*\*论点\*\*[：:]", arguments, flags=re.M)[1:]
    if not nodes:
        raise ValueError("分层章节需包含 - **论点**：，不能只提交聊天原文或空大纲")
    for node in nodes:
        for field in FIELDS:
            if not re.search(r"^\s+- \*\*" + field + r"\*\*[：:]\s*\S[^\n]*", node, re.M):
                raise ValueError(f"每个论点都需填写 {field}；缺证据时明确标记待补充")


def prepare(topic: str, pyramid_markdown: str, topic_id: str = "") -> dict:
    topic = topic.strip()
    if not topic or "\n" in topic or len(topic) > 200:
        raise ValueError("主题需为 1 到 200 字的单行标题")
    validate(pyramid_markdown)
    identity = resolve_topic_id(topic, topic_id)
    meta = {"schema": "writing-topic-v1", "topic": topic, "topic_id": identity}
    body = "<!-- writing-topic: " + json.dumps(meta, ensure_ascii=False) + " -->\n"
    body += f"# {topic}\n\n" + pyramid_markdown.strip() + "\n"
    config.TOPIC_DIR.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", topic)[:48].rstrip(". ")
    path = config.TOPIC_DIR / f"{date.today()}-{slug}-{uuid.uuid4().hex[:12]}.md"
    # 独占创建：同主题的新提炼也保留旧版本与人工修改。
    with path.open("x", encoding="utf-8", newline="\n") as f:
        f.write(body)
    return {"topic_file": str(path.resolve()), "topic_id": identity,
            "topic_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
            "markdown": body, "status": "topic_saved",
            "next_action": "展示主题文件路径与提炼内容；若用户已要求开始写作，调用 writing_start(topic_file=该路径)。保存主题不等于确认成稿或授权发布。"}


def load(topic_file: str) -> dict:
    if not topic_file:
        raise ValueError("请先调用 writing_prepare_topic 保存聊天的金字塔提炼，再传 topic_file 启动")
    path = Path(topic_file)
    if not path.is_absolute():
        path = config.BASE_DIR / path
    path = path.resolve()
    if not path.is_relative_to(config.TOPIC_DIR.resolve()) or path.suffix.lower() != ".md":
        raise ValueError("主题文件必须位于 writing/topic 目录内，且为 Markdown")
    if path.stat().st_size > config.TOPIC_MAX_BYTES + 4096:
        raise ValueError("主题文件过大")
    data = path.read_bytes()
    text = data.decode("utf-8-sig")
    first, _, body = text.partition("\n")
    try:
        meta = json.loads(first.strip().removeprefix("<!-- writing-topic: ").removesuffix(" -->").strip())
        if meta.get("schema") != "writing-topic-v1":
            raise ValueError()
        resolve_topic_id(meta["topic"], meta["topic_id"])
        if body.lstrip().splitlines()[0] != "# " + meta["topic"]:
            raise ValueError()
    except (ValueError, TypeError, KeyError, AttributeError, IndexError):
        raise ValueError("这不是已整理的主题文件，请通过 writing_prepare_topic 生成；原材料保持不变") from None
    validate(body)
    return {**meta, "topic_file": str(path), "idea": text,
            "topic_sha256": hashlib.sha256(data).hexdigest()}
