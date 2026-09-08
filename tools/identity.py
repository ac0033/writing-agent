"""主题身份不依赖标题或进程全局变量，旧会话可按原主题确定性恢复。"""
import hashlib
import re
import unicodedata


def topic_id(topic: str, explicit: str = "") -> str:
    if explicit:
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", explicit):
            raise ValueError("topic_id 需为小写字母、数字和连字符，最长 64 字符")
        return explicit
    normalized = " ".join(unicodedata.normalize("NFKC", topic).lower().split())
    return hashlib.sha256(normalized.encode()).hexdigest()[:16]


def topic_scope(state: dict) -> str:
    return "repo:writing-topic-" + topic_id(state.get("topic", ""), state.get("topic_id", ""))


def run_scope(state: dict) -> str:
    # 工作记忆每次运行一份，长期记忆仍按主题检索、沉淀。
    suffix = hashlib.sha256(state.get("thread_id", "unknown").encode()).hexdigest()[:16]
    return topic_scope(state) + "-run-" + suffix
