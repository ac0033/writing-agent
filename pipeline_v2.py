"""AI OS 调度：共享摘要、人工授权和审核版本由程序守护。"""
from __future__ import annotations

import hashlib
import json
import re
import time
from contextvars import ContextVar
from pathlib import Path

from langgraph.graph import StateGraph, START, END
from langgraph.types import interrupt

import config
import graph as legacy
from state import WritingState

SPECIALISTS = ("architect", "researcher", "writer", "reviewer", "stylist", "final_check")
# 服务层绑定只读队列快照函数；已消费ID进入checkpoint后，服务才ack。
feedback_reader = ContextVar("writing_v2_feedback_reader", default=None)
save_gate = ContextVar("writing_v2_save_gate", default=None)
runtime_settings_reader = ContextVar("writing_v2_runtime_settings_reader", default=None)


def _runtime_settings(state):
    reader = runtime_settings_reader.get()
    if reader is None:
        return {}
    delta = reader() or {}
    if not delta:
        return {}
    if delta.get("jev_settings") == state.get("jev_settings"):
        return {}
    # 当前文章内经用户确认的偏好不能被设置文件同步抹掉。
    current = [p for p in state.get("confirmed_preferences", []) if p.get("origin") == "article_user"]
    return {**delta, "confirmed_preferences": list(delta.get("confirmed_preferences", [])) + current}


def _fingerprint(state):
    return hashlib.sha256(json.dumps([state.get("shared_summary", ""), state.get("polished", "")],
                                    ensure_ascii=False).encode()).hexdigest()


def _review_fingerprint(state):
    return hashlib.sha256(json.dumps([state.get("shared_summary", ""), state.get("polished") or state.get("draft", "")],
                                    ensure_ascii=False).encode()).hexdigest()


def _json(text):
    value = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip()))
    if not isinstance(value, dict):
        raise ValueError("AI OS 必须返回 JSON 对象")
    return value


def _record(state, source, action, **details):
    return {"source": source, "action": action, "summary_version": state.get("summary_version", 0),
            "article_version": state.get("article_version", 0), **details}


def _invalidate():
    return {"review_verdict": "", "final_check_verdict": "", "publication_ready": False,
            "reviewed_version": -1, "checked_version": -1, "final_approved_version": -1,
            "forced_pass": False, "quality_issues": [], "claims": [], "approved_fingerprint": "",
            "reviewed_fingerprint": "", "checked_fingerprint": "", "review_dimensions": {}}


def _dimensions_pass(state):
    return all(state.get("review_dimensions", {}).get(k) == "pass" for k in ("intent", "facts", "reading"))


def summary(state):
    """模型只提出摘要；独立的人工节点才赋予确认状态。"""
    if config.MOCK_LLM:
        result, entry = state.get("user_idea", "") or state["topic"], {"node": "summary", "model": "mock", "thinking": "摘要测试"}
        if state.get("summary_feedback"):
            result += "\n作者修正：" + state["summary_feedback"]
    else:
        result, entry = legacy._run("orchestrator", "summary",
            legacy._load_prompt("summary.md"),
            json.dumps({"topic": state["topic"], "user_idea": state.get("user_idea", ""),
                        "previous_summary": state.get("shared_summary", ""),
                        "feedback": state.get("summary_feedback", "")}, ensure_ascii=False))
    return {"shared_summary": result, "summary_confirmed": False, "thinking_log": [entry],
            "summary_version": state.get("summary_version", 0) + 1, **_invalidate()}


def human_summary(state):
    pending = _drain_feedback(state)
    if pending:
        return {**pending, "summary_confirmed": False, "summary_feedback": pending.get("final_feedback", "")}
    decision = interrupt({"kind": "summary", "requires_human": True,
                          "summary": state["shared_summary"], "summary_version": state["summary_version"]})
    if not isinstance(decision, dict):
        decision = {"feedback": str(decision)}
    _check_expected_versions(state, decision)
    approved = decision.get("approved") is True or decision.get("route") == "approve"
    feedback = str(decision.get("feedback", "")).strip()
    if not approved and not feedback:
        raise ValueError("请确认摘要或提供修改意见")
    return {"summary_confirmed": approved, "summary_feedback": feedback,
            "decision_log": [_record(state, "user", "confirm_summary" if approved else "revise_summary", feedback=feedback)],
            "pipeline_version": "v2"}


def _ready(state):
    version = state.get("article_version", 0)
    return bool(state.get("summary_confirmed") and state.get("polished")
                and (not state.get("sample_requested") or state.get("sample_confirmed"))
                and not any(i.get("status") == "open" for i in state.get("issue_registry", {}).values())
                and not state.get("revision_pending")
                and _dimensions_pass(state)
                and state.get("review_verdict") == "pass" and state.get("reviewed_version") == version
                and state.get("final_check_verdict") == "pass" and state.get("checked_version") == version
                and state.get("reviewed_fingerprint") == _review_fingerprint(state)
                and state.get("checked_fingerprint") == _fingerprint(state)
                and state.get("publication_ready") and not state.get("quality_issues"))


def _check_expected_versions(state, decision):
    required = ("summary_version",) if decision.get("approved") is True else ()
    if decision.get("route") == "approve":
        required = ("summary_version", "article_version")
    for key in required:
        if type(decision.get("expected_" + key)) is not int:
            raise ValueError("批准必须提供所审阅版本：expected_" + key)
    for key in ("summary_version", "article_version"):
        expected = decision.get("expected_" + key)
        if expected is not None and (type(expected) is not int or expected != state.get(key, 0)):
            raise ValueError("用户确认对应的版本已经变化：" + key)


def _allowed(state):
    allowed = ["architect", "researcher"]
    # 新稿先走内容审核，避免用普通选择题提前包装为终审稿；预算暂停另有固定路径。
    review_passed_now = (state.get("draft") and state.get("review_verdict") == "pass" and _dimensions_pass(state)
                         and state.get("reviewed_version") == state.get("article_version"))
    # 审核已通过而成稿核验尚未跑完的稿子，技术路径是确定的（润色/核验）；此时不开放普通提问，
    # 否则 AI OS 会把“未核验的稿子”包装成终审稿请用户批准。预算暂停走固定路径，不受此限。
    if ((not (state.get("draft") or state.get("polished")) or state.get("reviewed_version") == state.get("article_version"))
            and not (review_passed_now and state.get("checked_version") != state.get("article_version"))):
        allowed.append("human_decision")
    if state.get("sample_requested") and not state.get("sample_confirmed"):
        allowed.append("sample")
    if any(i.get("status") == "open" and not i.get("dispute_reviewed") for i in state.get("issue_registry", {}).values()):
        allowed.append("independent_review")
    if state.get("outline"):
        allowed.append("writer")
    # 同一版本审核过一次就够了：通过了只能前进（润色/核验），没通过必须先改稿。重复审核只会消耗调度预算。
    if state.get("draft") and state.get("reviewed_version") != state.get("article_version"):
        allowed.append("reviewer")
    if (state.get("draft") and state.get("review_verdict") == "pass" and _dimensions_pass(state)
            and state.get("reviewed_version") == state.get("article_version")):
        if not state.get("polished") or state.get("revision_pending"):
            allowed.append("stylist")
        # 核验判定通过但程序校验（引文定位/链接）未过时，问题出在核验产物本身，可再核验；同一问题两次未变会由预算规则暂停。
        if state.get("polished") and (state.get("checked_version") != state.get("article_version")
                                      or (state.get("final_check_verdict") == "pass" and not state.get("publication_ready"))):
            allowed.append("final_check")
    if _ready(state):
        allowed.append("human_final")
    if review_passed_now and not state.get("revision_pending") and state.get("checked_version") != state.get("article_version"):
        # 审核通过的稿子只能前进：重新定框架会作废正文，重复审核/研究只耗预算，普通提问会被用来包装终审。
        allowed = [a for a in allowed if a in ("stylist", "final_check", "human_final")]
    return allowed


def _mock_action(state):
    if state.get("sample_requested") and not state.get("sample_confirmed"):
        return "sample"
    if not state.get("outline"):
        return "architect"
    if not state.get("research_completed"):
        return "researcher"
    if not state.get("draft") or state.get("review_verdict") == "fail" or state.get("revision_pending"):
        return "writer"
    if state.get("reviewed_version") != state.get("article_version"):
        return "reviewer"
    if not state.get("polished"):
        return "stylist"
    if state.get("checked_version") != state.get("article_version"):
        return "final_check"
    return "human_final" if _ready(state) else "writer"


def _decision_actions(state):
    actions = _allowed(state)
    if state.get("jev_settings", {}).get("mode", getattr(config, "JEV_MODE", "off")) in ("shadow", "enabled"):
        actions.append("jev")
    return actions


def _jev_conflict_entry(conflicts, request):
    identity = request.get("decision_id")
    if identity in conflicts:
        return conflicts[identity]
    # 同一问题换掉模型生成的标识，不得重置重评次数。
    question = re.sub(r"\s+", "", str(request.get("question", "")))
    if not question:
        return None
    return next((entry for entry in conflicts.values()
                 if entry.get("category") == request.get("category")
                 and re.sub(r"\s+", "", str(entry.get("question", ""))) == question), None)


def _history_context(state):
    """完整历史保留在检查点；调度只看近期任务和结论，不重复发送历次全文。"""
    history = []
    for item in state.get("node_history", [])[-8:]:
        result = item.get("result", {})
        compact = {k: item.get(k) for k in ("node", "summary_version", "input_article_version", "output_article_version")}
        compact["instruction"] = str(item.get("instruction", ""))[:1600]
        compact["conclusions"] = {k: str(result[k])[:1200] for k in (
            "review_verdict", "review_dimensions", "review_comments", "final_check_verdict",
            "quality_issues", "research_gaps", "status", "action", "reason") if k in result}
        history.append(compact)
    return history


def ai_os(state):
    runtime = _runtime_settings(state)
    if runtime:
        return {**runtime, "ai_os_next": "ai_os"}
    pending = _drain_feedback(state)
    if pending:
        return {**pending, "ai_os_next": "ai_os"}
    if not state.get("summary_confirmed"):
        raise ValueError("尚未确认摘要，不能启动专业节点")
    max_steps = getattr(config, "AI_OS_MAX_STEPS", 36) + state.get("additional_steps", 0)
    registry = state.get("issue_registry", {})
    repeated = max(state.get("failure_counts", {}).values(), default=0) if not registry else 0
    revision_limit = getattr(config, "AI_OS_MAX_ISSUE_REVISIONS", 2) + state.get("additional_revisions", 0)
    time_exhausted = state.get("execution_seconds", 0) >= getattr(config, "AI_OS_MAX_SECONDS", 14400) + state.get("additional_seconds", 0)
    blocked = [i for i in registry.values() if i.get("status") == "open" and (
        (i.get("revision_attempts", 0) >= revision_limit and i.get("last_seen_version") == state.get("article_version", 0))
        or i.get("unchanged_returns", 0) >= 2)]
    if time_exhausted or state.get("ai_os_steps", 0) >= max_steps or repeated > revision_limit or blocked:
        reason = "总执行时间预算已用完" if time_exhausted else ("总调度预算已用完" if state.get("ai_os_steps", 0) >= max_steps else "同类审核问题两次修订后仍未解决")
        return {"ai_os_next": "human_decision", "human_question": reason + ("\n" + json.dumps(blocked, ensure_ascii=False) if blocked else ""),
                "pause_reason": reason, "task_instruction": "", "publication_ready": False}
    allowed = _decision_actions(state)
    if config.MOCK_LLM:
        decision = {"action": _mock_action(state), "instruction": "依照已确认摘要完成工作", "reason": "mock 调度"}
        entry = {"node": "ai_os", "model": "mock", "thinking": "测试调度"}
    else:
        context = {k: state.get(k) for k in (
            "topic", "user_idea", "shared_summary", "summary_version", "article_version", "outline", "research_brief",
            "materials", "research_gaps", "research_request", "draft", "polished", "review_verdict", "review_comments",
            "final_check_verdict", "final_check_comments", "quality_issues", "final_feedback",
            "review_dimensions", "reviewed_version", "checked_version", "publication_ready", "final_approved_version",
            "decision_log", "failure_counts", "ai_os_steps", "jev_result", "revision_pending",
            "issue_registry", "pending_explorations", "confirmed_preferences", "jev_conflicts", "style_references")}
        context["node_history"] = _history_context(state)
        context["allowed_actions"] = allowed
        context["jev_mode"] = state.get("jev_settings", {}).get("mode", getattr(config, "JEV_MODE", "off"))
        context["jev_policy"] = "shadow只记录旁路预测，不能等待其代决；已有权限的工作由AI OS继续完成。"
        context["remaining_steps"] = max_steps - state.get("ai_os_steps", 0)
        context["remaining_issue_revisions"] = {key: max(0, revision_limit - issue.get("revision_attempts", 0))
            for key, issue in registry.items() if issue.get("status") == "open"}
        context["response_schema"] = {
            "type": "object", "required": ["action", "instruction", "reason", "issue_ids", "question"],
            "properties": {
                "action": {"type": "string", "enum": allowed},
                "instruction": {"type": "string"}, "reason": {"type": "string"},
                "issue_ids": {"type": "array", "items": {"type": "string"},
                    "description": "由AI OS在自己的顶层JSON填写；writer/stylist修订时必须列出本次处理的已有问题，其他动作可为空。不能让下游writer代填。"},
                "question": {"type": "string", "description": "human_decision时填写具体问题、选项和建议；其他动作可为空。"}}}
        result, entry = legacy._run("orchestrator", "ai_os",
            legacy._load_prompt("orchestrator.md"),
            json.dumps(context, ensure_ascii=False))
        decision = _json(result)
    action = decision.get("action")
    if action not in allowed:
        raise ValueError(f"AI OS 调度不合法：{action}；允许：{allowed}")
    record = _record(state, "ai_os", action, reason=decision.get("reason", ""), instruction=decision.get("instruction", ""))
    update = {"ai_os_next": action, "task_instruction": str(decision.get("instruction", "")),
              "thinking_log": [entry], "decision_log": [record], "ai_os_steps": state.get("ai_os_steps", 0) + 1,
              "human_question": str(decision.get("question", "")), "pause_reason": ""}
    issue_ids = decision.get("issue_ids", [])
    if config.MOCK_LLM and action in ("writer", "stylist"):
        issue_ids = [key for key, issue in registry.items() if issue.get("status") == "open"]
    if not isinstance(issue_ids, list) or any(key not in registry or registry[key].get("status") != "open" for key in issue_ids):
        raise ValueError("修订问题必须引用当前未解决问题标识")
    # 只有在当前稿上仍被报告的问题才要求定向修订声明；旧版本遗留、尚待成稿核验复核的条目不阻塞正常润色，
    # 也不能被润色悄悄“解决”——它们仍由下一次成稿核验裁定。
    current = state.get("article_version", 0)
    pending = [key for key, issue in registry.items() if issue.get("status") == "open" and issue.get("last_seen_version") == current]
    if action in ("writer", "stylist") and pending and not issue_ids:
        raise ValueError("针对性修订必须声明 issue_ids：" + "，".join(pending))
    update["active_issue_ids"] = issue_ids
    if action == "independent_review":
        dispute = decision.get("dispute", {})
        if not isinstance(dispute, dict) or dispute.get("issue_id") not in registry or not str(dispute.get("reason", "")).strip():
            raise ValueError("独立复核必须提供具体问题与争议理由")
        if registry[dispute["issue_id"]].get("dispute_reviewed"):
            raise ValueError("同一问题只能申请一次独立复核")
        update["review_dispute"] = dispute
    if action == "human_decision" and (not isinstance(decision.get("question"), str) or not decision["question"].strip()):
        raise ValueError("返回用户必须提供具体问题、选项和建议")
    if decision.get("jev_conflict"):
        conflict = decision["jev_conflict"]
        prior = state.get("jev_result", {})
        decision_id = prior.get("decision_id")
        if not decision_id or not isinstance(conflict, dict) or not conflict.get("reason") or not conflict.get("constraint"):
            raise ValueError("JEV冲突必须绑定已有决定并指出明确要求或事实依据")
        conflicts = dict(state.get("jev_conflicts", {}))
        original = state.get("jev_request", {})
        conflicts[decision_id] = {**conflicts.get(decision_id, {}),
            "reason": conflict["reason"], "constraint": conflict["constraint"],
            "question": original.get("question", ""), "category": original.get("category")}
        update["jev_conflicts"] = conflicts
        update["jev_result"] = {**prior, "executable": False, "reason": conflict["reason"]}
        update["decision_log"].append(_record(state, "ai_os", "jev_conflict", decision_id=decision_id, **conflict))
    if action == "jev":
        request = decision.get("decision_request")
        if not isinstance(request, dict):
            raise ValueError("JEV 请求必须包含结构化候选方案和依据")
        conflict = _jev_conflict_entry(update.get("jev_conflicts", state.get("jev_conflicts", {})), request)
        if conflict and conflict.get("reevaluations", 0) >= 1:
            raise ValueError("JEV冲突最多重评一次")
        if conflict and not request.get("supplemental_evidence"):
            raise ValueError("JEV冲突重评前必须补充材料")
        update["jev_request"] = {**request, "shared_summary": state["shared_summary"],
                                 "summary_version": state.get("summary_version", 0),
                                 "article_version": state.get("article_version", 0)}
    return update


def specialist(name):
    def run(state):
        if not state.get("summary_confirmed") or name not in _allowed(state):
            raise ValueError("节点前置条件不满足：" + name)
        # 原始输入只在本次调用副本附加摘要，持久化原文不变。
        local = dict(state)
        local["user_idea"] = (state.get("user_idea", "") + "\n\n【当前已确认摘要，优先依据】\n" + state["shared_summary"])
        local["revision_feedback"] = ("【当前已确认摘要】\n" + state["shared_summary"]
                                       + "\n【AI OS 本次任务】\n" + state.get("task_instruction", ""))
        if name == "architect":
            local["outline_feedback"] = ["AI OS 的组织要求（不是作者新增立场）：" + state.get("task_instruction", "")]
        if name == "researcher" and state.get("task_instruction"):
            local["research_request"] = "\n".join(filter(None, (state.get("research_request", ""), state["task_instruction"])))
        delegated = state.get("jev_result", {})
        if (delegated.get("executable") and delegated.get("summary_revision") == state.get("summary_version", 0)
                and delegated.get("article_revision") == state.get("article_version", 0)):
            local["revision_feedback"] += "\n" + delegated.get("execution_instruction", "")
        if name == "reviewer" and state.get("polished"):
            local["draft"] = state["polished"]
        local["revision_feedback"] += "\n【当前问题记录，复核沿用标识】\n" + json.dumps(state.get("issue_registry", {}), ensure_ascii=False)
        update = getattr(legacy, name)(local)
        version = state.get("article_version", 0)
        counts = dict(state.get("failure_counts", {}))
        if name in ("writer", "stylist"):
            output_key = "draft" if name == "writer" else "polished"
            current_text = state.get("polished") or state.get("draft", "")
            if (state.get("revision_pending") or state.get("active_issue_ids")) and update.get(output_key, "").strip() == current_text.strip():
                raise ValueError("节点未实际修改稿件，不能宣称已处理用户反馈")
            registry = {key: dict(value) for key, value in state.get("issue_registry", {}).items()}
            for key in state.get("active_issue_ids", []):
                if key in registry and registry[key].get("status") == "open":
                    registry[key]["revision_attempts"] = registry[key].get("revision_attempts", 0) + 1
                    registry[key]["attempts"] = registry[key].get("attempts", []) + [{"node": name, "instruction": state.get("task_instruction", ""), "article_version": version + 1}]
            update["issue_registry"] = registry
            version += 1
            update.update(_invalidate())
            update["article_version"] = version
            update["revision_pending"] = False
            if name == "writer":
                update["polished"] = ""
            update["revision_feedback"] = ""
            update["final_feedback"] = ""
        elif name in ("architect", "researcher"):
            update.update(_invalidate())
            if name == "architect":
                # 重新定框架后必须重写；旧正文保留在 node_history。
                update.update(draft="", polished="", research_completed=False)
            else:
                update["research_completed"] = True
        elif name == "reviewer":
            update["forced_pass"] = False
            update["reviewed_version"] = version
            update["reviewed_fingerprint"] = _review_fingerprint(state)
            failed = update.get("review_verdict") != "pass" or not _dimensions_pass(update)
            if failed:
                update["review_verdict"] = "fail"
            counts["reviewer"] = counts.get("reviewer", 0) + 1 if failed else 0
            update.update(publication_ready=False, checked_version=-1, final_approved_version=-1)
        elif name == "final_check":
            update["checked_version"] = version
            update["checked_fingerprint"] = _fingerprint(state)
            failed = not update.get("publication_ready") or update.get("final_check_verdict") != "pass"
            counts["final_check"] = counts.get("final_check", 0) + 1 if failed else 0
        if name in ("reviewer", "final_check"):
            update["issue_registry"] = _sync_issues(state, update, name, failed)
        update["failure_counts"] = counts
        update["node_history"] = [{"node": name, "summary_version": state.get("summary_version", 0),
                                    "input_article_version": state.get("article_version", 0),
                                    "output_article_version": version, "instruction": state.get("task_instruction", ""),
                                    "result": {k: v for k, v in update.items() if k not in ("thinking_log", "node_history")}}]
        return update
    return run


_STATUS_LINES = {"润色后事实与表达复核未通过", "初稿审核尚未通过", "最终核验结果无法解析，需要重新核验",
                 "最终核验没有覆盖全部引用链接的具体论断", "最终核验未登记任何核心论断，不能据空清单判通过"}
_ISSUE_SOURCES = ("reviewer", "final_check")


def _sync_issues(state, update, source, failed):
    """审核保持稳定问题标识；只有审核或独立复核能解决问题。"""
    registry = {key: dict(value) for key, value in state.get("issue_registry", {}).items()}
    comments = str(update.get("review_comments" if source == "reviewer" else "final_check_comments", ""))
    raw = list(update.get("quality_issues", [])) if source == "final_check" else []
    # quality_issues 同时承担“能否发布”的全部阻塞原因；其中研究缺口说明和结论性状态行不是稿件里可修订的问题，
    # 登记进来只会让调度看到一堆无法由 writer/stylist 解决的“未解决问题”。它们仍留在 quality_issues 里阻塞发布。
    gaps = {str(g) for g in state.get("research_gaps", [])}
    raw = [item for item in raw if not (isinstance(item, str) and (item in gaps or item in _STATUS_LINES))]
    raw += re.findall(r"^ISSUE:\s*(\{.*\})\s*$", comments, re.M)
    parsed = []
    for item in raw:
        if isinstance(item, dict):
            issue = item
        else:
            try:
                issue = json.loads(item)
            except (ValueError, TypeError):
                issue = {"text": str(item)}
                marker = re.match(r"\[ISSUE:([^\]]+)\]\s*(.*)", str(item), re.S)
                if marker:
                    issue = {"id": marker.group(1), "text": marker.group(2)}
        if not isinstance(issue, dict):
            issue = {"text": str(item)}
        text = str(issue.get("text", "")).strip()
        if text:
            parsed.append({k: v for k, v in {**issue, "text": text}.items()
                           if k in {"id", "text", "category", "location", "suggested_node", "resolution_criteria", "criterion"}})
    if failed and not parsed:
        parsed = [{"text": comments or "审核缺少完整通过结论"}]
    seen_keys = set()
    for issue in parsed if failed else []:
        local_id = str(issue.get("id") or hashlib.sha256(issue["text"].encode()).hexdigest()[:16])
        # 已有完整标识优先于当前审核节点命名空间；跨节点复核是在追踪同一问题。
        # 尤其原审核已暂判解决、成稿核验发现残留时，应重开原问题并沿用修订预算。
        known_id = local_id in registry
        if not known_id:
            # 模型有时给新问题套上别的审核节点的前缀（如 reviewer 报出 final_check:xxx）。
            # 那不是已登记的问题，按当前节点的新问题登记，不叠成 reviewer:final_check:xxx。
            while any(local_id.startswith(prefix + ":") for prefix in _ISSUE_SOURCES if prefix != source):
                local_id = local_id.split(":", 1)[1]
            known_id = local_id in registry
        key = local_id if known_id or local_id.startswith(source + ":") else source + ":" + local_id
        # 相同问题改标识仍沿用计数；语义不同但未说明旧问题解决，也不能遗忘旧问题。
        normalized = re.sub(r"\W+", "", issue["text"]).casefold()
        alias = next((k for k, value in registry.items() if value.get("source") == source
                      and re.sub(r"\W+", "", value.get("text", "")).casefold() == normalized), None)
        if alias and not known_id:
            key = alias
        seen_keys.add(key)
        previous = registry.get(key, {})
        origin = previous.get("source", source)
        review_sources = list(dict.fromkeys([origin, *previous.get("review_sources", []), source]))
        same_version = previous.get("last_seen_version") == state.get("article_version", 0)
        registry[key] = {**previous, **issue, "id": key, "source": origin, "status": "open",
                         "review_sources": review_sources, "last_reported_by": source,
                         "revision_attempts": previous.get("revision_attempts", 0),
                         "unchanged_returns": previous.get("unchanged_returns", 0) + 1 if same_version else 0,
                         "last_seen_version": state.get("article_version", 0)}
    if not failed:
        for issue in registry.values():
            if issue.get("source") == source or source in issue.get("review_sources", []):
                issue.update(status="resolved", resolved_version=state.get("article_version", 0))
    else:
        # 忽略未再列出的旧问题不等于解决；必须明确记录复核结果。
        resolved_ids = re.findall(r"^RESOLVED:\s*(\S+)\s*$", comments, re.M)
        if source == "final_check":
            try:
                resolved_ids += _json(comments).get("resolved_issues", [])
            except (ValueError, TypeError):
                pass
        for key in resolved_ids:
            if key in registry and (registry[key].get("source") == source or source in registry[key].get("review_sources", [])) and key not in seen_keys:
                registry[key].update(status="resolved", resolved_version=state.get("article_version", 0))
        for key, issue in registry.items():
            if (issue.get("source") == source or source in issue.get("review_sources", [])) and issue.get("status") == "open" and key not in seen_keys:
                issue["last_seen_version"] = state.get("article_version", 0)
                issue["resolution_missing"] = True
    return registry


def independent_review(state):
    """新角色调用处理一次具体争议；不覆盖原审核门，结论回传下一次完整审核。"""
    dispute = state.get("review_dispute", {})
    key = dispute.get("issue_id")
    registry = {k: dict(v) for k, v in state.get("issue_registry", {}).items()}
    issue = registry.get(key)
    if not issue or issue.get("dispute_reviewed") or issue.get("status") != "open":
        raise ValueError("独立复核只接受尚未复核的当前问题")
    context = {k: state.get(k) for k in ("user_idea", "shared_summary", "draft", "polished", "materials", "source_records")}
    context.update(issue=issue, dispute=dispute)
    if config.MOCK_LLM:
        result, entry = {"resolved": False, "reason": "mock维持原问题", "evidence": []}, {"node": "independent_review", "model": "mock", "thinking": "模拟独立复核，维持原问题"}
    else:
        raw, entry = legacy._run("final_check", "independent_review", legacy._load_prompt("independent_review.md"), json.dumps(context, ensure_ascii=False))
        result = _json(raw)
    if type(result.get("resolved")) is not bool or not result.get("reason") or not isinstance(result.get("evidence"), list):
        raise ValueError("独立复核必须给出结论、理由和证据列表")
    if result["resolved"] and not result["evidence"]:
        raise ValueError("独立复核不能无证据消除失败结论")
    from tools.research_materials import evidence_texts
    # 问题描述、争议理由和模型摘要不是事实证据。只在输入原文与已读来源核对。
    sources = {"user_idea": [str(state.get("user_idea", ""))],
               "shared_summary": [str(state.get("shared_summary", ""))]}
    for material in state.get("materials", []):
        sources.setdefault(material.get("source_url", ""), []).extend(evidence_texts(material))
    if issue.get("category") == "reading":
        sources["current_article"] = [str(state.get("polished") or state.get("draft", ""))]
    if any(not isinstance(e, dict) or not isinstance(e.get("quote"), str) or not e["quote"]
           or not e.get("source") or not any(e["quote"] in text for text in sources.get(e["source"], []))
           for e in result["evidence"]):
        raise ValueError("独立复核引用必须能在输入材料定位")
    # 全文审核必须继续；不能以单个问题复核代替整稿核验。
    issue.update(dispute_reviewed=True, dispute_result=result)
    if result["resolved"]:
        issue.update(status="resolved", resolved_version=state.get("article_version", 0))
    return {"issue_registry": registry, "thinking_log": [entry],
            "decision_log": [_record(state, "independent_review", "resolve" if result["resolved"] else "uphold", issue_id=key, result=result)],
            "node_history": [{"node": "independent_review", "result": result}]}


def sample(state):
    """由写作节点生成同一内容的两种短样稿，AI OS不得代写。"""
    if not state.get("summary_confirmed") or not state.get("sample_requested") or state.get("sample_confirmed"):
        raise ValueError("短样稿仅用于已确认摘要且主动启用的风格对齐")
    if config.MOCK_LLM:
        result = {"A": "先提出问题，再展开核心论述。", "B": "先介绍具体情境，再展开同一核心论述。"}
        entry = {"node": "sample", "model": "mock", "thinking": "模拟两种短样稿，等待真实用户选择"}
    else:
        system = legacy._load_prompt("sample.md")
        system += "\n" + legacy._read_prompt_file(config.PROMPTS_DIR / "skills/article-writing/SKILL.md", "project_adaptation")
        raw, entry = legacy._run("writer", "sample", system, json.dumps({
            "shared_summary": state["shared_summary"], "source_text": state.get("user_idea", ""),
            "feedback": state.get("sample_feedback", ""), "materials": state.get("materials", [])}, ensure_ascii=False))
        result = _json(raw)
    if set(result) != {"A", "B"} or any(not isinstance(v, str) or not v.strip() for v in result.values()):
        raise ValueError("短样稿必须包含A、B两个非空写法")
    return {"sample_options": result, "sample_summary_version": state.get("summary_version", 0), "thinking_log": [entry],
            "node_history": [{"node": "sample", "result": result}]}


def human_sample(state):
    pending = _drain_feedback(state)
    if pending:
        return {**pending, "sample_confirmed": False}
    decision = interrupt({"kind": "sample", "requires_human": True, "options": state["sample_options"],
                          "summary_version": state.get("summary_version", 0),
                          "question": "选择更合适的短样稿，或用自然语言说明希望调整的语气与展开方式。"})
    if not isinstance(decision, dict):
        decision = {"feedback": str(decision)}
    choice, feedback = decision.get("choice"), str(decision.get("feedback", "")).strip()
    _check_expected_versions(state, decision)
    if state.get("sample_summary_version") != state.get("summary_version", 0):
        raise ValueError("短样稿依据的摘要已变化，请重新生成")
    if choice not in state["sample_options"]:
        if not feedback:
            raise ValueError("请选择短样稿或提供修改意见")
        return {"sample_feedback": feedback, "sample_confirmed": False,
                **apply_user_update(state, feedback), "decision_log": [_record(state, "user", "revise_sample", feedback=feedback)]}
    chosen = state["sample_options"][choice]
    text = "本篇采用已确认短样稿" + choice + "的语气和展开方式；仅确认表达方式，不新增观点。样稿：\n" + chosen
    preference = {"id": "sample-" + str(state.get("summary_version", 0)), "source": "user", "origin": "article_user", "scope": state.get("topic_id", ""),
                  "category": "tone", "text": text, "evidence": {"choice": choice, "feedback": feedback}}
    delta = apply_user_update(state, text + ("\n作者补充：" + feedback if feedback else ""))
    preferences = list(delta.get("confirmed_preferences", state.get("confirmed_preferences", [])))
    preferences = [p for p in preferences if not str(p.get("id", "")).startswith("sample-")]
    preferences.append(preference)
    return {**delta, "sample_confirmed": True, "confirmed_preferences": preferences,
            "decision_log": [_record(state, "user", "confirm_sample", preference=preference)]}


def jev(state):
    from jev_adapter import evaluate_decision
    runtime = _runtime_settings(state)
    state = {**state, **runtime}
    request = dict(state["jev_request"])
    # 外发授权和用户偏好来源来自程序状态，绝不信任模型自填的授权。
    request["external_authorization"] = state.get("jev_external_authorization", {})
    if "jev_settings" in state:
        request["settings"] = state["jev_settings"]
        request["external_authorization"] = state["jev_settings"].get("authorizations", {}).get(request.get("decision_id"), {})
    request["preferences"] = state.get("confirmed_preferences", [])
    request["summary_revision"] = state.get("summary_version", 0)
    request["article_revision"] = state.get("article_version", 0)
    conflicts = {k: dict(v) for k, v in state.get("jev_conflicts", {}).items()}
    conflict = _jev_conflict_entry(conflicts, request)
    if conflict:
        if conflict.get("reevaluations", 0) >= 1 or not request.get("supplemental_evidence"):
            raise ValueError("JEV冲突必须补充材料且最多重评一次")
        conflict["reevaluations"] = conflict.get("reevaluations", 0) + 1
    result = evaluate_decision(request)
    if not isinstance(result, dict):
        raise ValueError("JEV 适配器返回必须为对象")
    if result.get("executable"):
        versions_match = (result.get("summary_revision") == request["summary_revision"]
                          and result.get("article_revision") == request["article_revision"])
        candidate = next((c for c in request.get("candidates", [])
                          if c.get("id") == result.get("candidate_id") and c.get("eligible") is True), None)
        if not versions_match or not candidate:
            result = {**result, "executable": False, "action": "need_analysis", "reason": "版本或候选方案不匹配"}
        else:
            result = {**result, "execution_instruction": "已获授权的 JEV 选择，后续执行须遵循：" + str(candidate.get("text", ""))}
    return {**runtime, "jev_result": result, "jev_conflicts": conflicts, "decision_log": [_record(state, "jev", "decision", result=result)],
            "node_history": [{"node": "jev", "result": result}]}


def apply_user_update(state, feedback="", summary_text=None):
    """用户反馈统一入口；服务只在节点边界消费队列后调用，不能由模型调用。"""
    started = time.monotonic()
    feedback = str(feedback).strip()
    if not feedback and not summary_text:
        raise ValueError("用户更新不能为空")
    entry = None
    if summary_text:
        result = {"summary": str(summary_text), "explicit_update": True, "explorations": []}
    elif config.MOCK_LLM:
        exploratory = bool(re.search(r"[？?]|是否|要不要|也许|考虑一下", feedback))
        result = {"summary": state["shared_summary"] if exploratory else state["shared_summary"] + "\n作者明确更新：" + feedback,
                  "explicit_update": not exploratory, "explorations": [feedback] if exploratory else []}
    else:
        raw, entry = legacy._run("orchestrator", "summary_update", legacy._load_prompt("summary_update.md"),
            json.dumps({"current_summary": state["shared_summary"], "feedback": feedback,
                        "source_text": state.get("user_idea", "")}, ensure_ascii=False))
        result = _json(raw)
    if not isinstance(result.get("summary"), str) or not result["summary"].strip() or type(result.get("explicit_update")) is not bool:
        raise ValueError("摘要更新必须返回非空摘要和明确更新判断")
    if not isinstance(result.get("explorations", []), list):
        raise ValueError("探索性想法必须为列表")
    changed = result["explicit_update"]
    explorations = list(state.get("pending_explorations", []))
    for item in result.get("explorations", []):
        if not isinstance(item, str) or not item.strip():
            raise ValueError("探索性想法必须为非空文本")
        if item not in explorations:
            explorations.append(item)
    update = {"final_feedback": feedback, "pending_explorations": explorations,
              "shared_summary": result["summary"] if changed else state["shared_summary"],
              "summary_version": state.get("summary_version", 0) + int(changed)}
    if changed:
        update.update(revision_pending=True, **_invalidate())
        preferences = list(state.get("confirmed_preferences", []))
        for preference in result.get("preferences", []):
            if not isinstance(preference, dict) or not preference.get("quote") or preference["quote"] not in feedback:
                raise ValueError("偏好回写必须引用本次用户原话")
            category = preference.get("category")
            if category not in ("tone", "opening", "narrative_order", "equivalent_example"):
                raise ValueError("未知偏好类别")
            scope = state.get("topic_id", "")
            preferences = [p for p in preferences if not (p.get("category") == category and p.get("scope") == scope)]
            preferences.append({"id": "user-" + hashlib.sha256((category + feedback).encode()).hexdigest()[:16],
                                "source": "user", "origin": "article_user", "scope": scope, "category": category,
                                "text": preference["quote"], "summary_version": update["summary_version"]})
        update["confirmed_preferences"] = preferences
    if entry:
        update["thinking_log"] = [entry]
        update["execution_seconds"] = state.get("execution_seconds", 0) + time.monotonic() - started
    return update


def _drain_feedback(state):
    reader = feedback_reader.get()
    if reader is None:
        return {}
    processed = {d.get("user_feedback_id") for d in state.get("decision_log", []) if d.get("user_feedback_id")}
    local = dict(state)
    update, records, thinking = {}, [], []
    for item in reader() or []:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]:
            raise ValueError("反馈队列条目必须有非空字符串id")
        if item["id"] in processed:
            continue
        delta = apply_user_update(local, item.get("text", ""))
        thinking.extend(delta.get("thinking_log", []))
        records.append(_record(local, "user", "live_feedback", user_feedback_id=item["id"], feedback=item["text"]))
        local.update(delta)
        update.update(delta)
        processed.add(item["id"])
    if records:
        update["decision_log"] = records
    if thinking:
        update["thinking_log"] = thinking
    return update


def human_decision(state):
    decision = interrupt({"kind": "decision", "requires_human": True,
                          "question": state.get("human_question", ""), "pause_reason": state.get("pause_reason", ""),
                          "summary": state.get("shared_summary", ""), "ai_os_steps": state.get("ai_os_steps", 0),
                          "review_comments": state.get("review_comments", ""),
                          "quality_issues": state.get("quality_issues", [])})
    if not isinstance(decision, dict):
        decision = {"feedback": str(decision)}
    feedback = str(decision.get("feedback", "")).strip()
    extra = decision.get("additional_steps", 0)
    extra_seconds = decision.get("additional_seconds", 0)
    extra_revisions = decision.get("additional_revisions", 0)
    if not isinstance(extra, int) or isinstance(extra, bool) or not 0 <= extra <= 100:
        raise ValueError("additional_steps 必须是0至100的整数")
    if not isinstance(extra_seconds, int) or isinstance(extra_seconds, bool) or not 0 <= extra_seconds <= 86400:
        raise ValueError("additional_seconds 必须是0至86400的整数")
    if not isinstance(extra_revisions, int) or isinstance(extra_revisions, bool) or not 0 <= extra_revisions <= 10:
        raise ValueError("additional_revisions 必须是0至10的整数")
    if not feedback and not decision.get("summary") and not extra and not extra_seconds and not extra_revisions:
        raise ValueError("请提供决定、修改要求或明确增加预算")
    update = {"final_feedback": feedback, "pause_reason": "", "human_question": "",
              "additional_steps": state.get("additional_steps", 0) + extra,
              "additional_seconds": state.get("additional_seconds", 0) + extra_seconds,
              "additional_revisions": state.get("additional_revisions", 0) + extra_revisions,
              "decision_log": [_record(state, "user", "decision", feedback=feedback, additional_steps=extra,
                                       additional_seconds=extra_seconds, additional_revisions=extra_revisions)]}
    # 用户明确纠正直接生效；模型不能使用本入口替用户更新摘要。
    if decision.get("summary") or feedback:
        update.update(apply_user_update(state, feedback, decision.get("summary")))
    return update


def human_final(state):
    pending = _drain_feedback(state)
    if pending:
        return {**pending, "final_route": "feedback", "ai_os_next": "ai_os"}
    if not _ready(state):
        raise ValueError("当前版本尚未通过全部审核，不能交人工终审")
    decision = interrupt({"kind": "final", "pipeline_version": "v2", "requires_human": True, "polished": state["polished"],
                          "publication_ready": True, "quality_issues": [], "forced_pass": False,
                          "summary": state["shared_summary"], "summary_version": state.get("summary_version", 0),
                          "article_version": state["article_version"],
                          "final_check_comments": state.get("final_check_comments", "")})
    if not isinstance(decision, dict):
        decision = {"feedback": str(decision)}
    _check_expected_versions(state, decision)
    if decision.get("route") == "approve":
        return {"final_route": "approve", "final_approved_version": state["article_version"],
                "approved_fingerprint": _fingerprint(state),
                "decision_log": [_record(state, "user", "approve_final")]}
    feedback = str(decision.get("feedback", "")).strip()
    if not feedback:
        raise ValueError("请明确确认或给出自然语言修改意见")
    return {"final_route": "feedback", **apply_user_update(state, feedback),
            "decision_log": [_record(state, "user", "revise_final", feedback=feedback)]}


def save(state):
    gate = save_gate.get()
    pending = gate(state) if gate else _drain_feedback(state)
    if pending:
        return {**pending, "final_route": "feedback", "ai_os_next": "ai_os"}
    if (not _ready(state) or state.get("final_approved_version") != state.get("article_version")
            or state.get("approved_fingerprint") != _fingerprint(state)):
        raise ValueError("只能保存用户已确认的当前通过版本")
    result = legacy.save(state)
    output = Path(result["output_path"]).parent
    (output / "shared_summary.md").write_text(state["shared_summary"], encoding="utf-8")
    (output / "decisions.json").write_text(json.dumps(state.get("decision_log", []), ensure_ascii=False, indent=2), encoding="utf-8")
    (output / "pipeline_v2.json").write_text(json.dumps({k: state.get(k) for k in (
        "pipeline_version", "summary_version", "article_version", "ai_os_steps", "node_history", "issue_registry",
        "confirmed_preferences", "sample_options", "sample_confirmed", "jev_conflicts")}, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def _timed(fn):
    def run(state):
        start = time.monotonic()
        result = fn(state)
        result["execution_seconds"] = state.get("execution_seconds", 0) + time.monotonic() - start
        return result
    return run


def build_graph_v2(checkpointer=None):
    builder = StateGraph(WritingState)
    for name, fn in (("summary", summary), ("human_summary", human_summary), ("ai_os", ai_os),
                     ("sample", sample), ("human_sample", human_sample), ("independent_review", independent_review),
                     ("human_decision", human_decision), ("human_final", human_final), ("jev", jev), ("save", save)):
        builder.add_node(name, fn if name.startswith("human_") else _timed(fn))
    for name in SPECIALISTS:
        builder.add_node(name, _timed(specialist(name)))
        builder.add_edge(name, "ai_os")
    builder.add_edge(START, "summary")
    builder.add_edge("summary", "human_summary")
    builder.add_conditional_edges("human_summary", lambda s: "ai_os" if s.get("summary_confirmed") else "summary")
    builder.add_conditional_edges("ai_os", lambda s: s["ai_os_next"])
    builder.add_edge("jev", "ai_os")
    builder.add_edge("sample", "human_sample")
    builder.add_edge("human_sample", "ai_os")
    builder.add_edge("independent_review", "ai_os")
    builder.add_edge("human_decision", "ai_os")
    builder.add_conditional_edges("human_final", lambda s: "save" if s.get("final_route") == "approve" else "ai_os")
    builder.add_conditional_edges("save", lambda s: "ai_os" if s.get("final_route") == "feedback" else END)
    return builder.compile(checkpointer=checkpointer)
