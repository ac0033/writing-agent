"""JEV 用户设置与评估记录。只处理本地数据，不调用模型，不自动开放代决。"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone

from jev_adapter import ALLOWED_CATEGORIES, RESERVED


def default_settings():
    return {"version": 1, "mode": "off", "preferences": [], "categories": {},
            "authorizations": {}, "revocations": [], "audit": []}


def _text(data, key):
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"缺少 {key}")
    return value


def content_fingerprint(request):
    """授权绑定发送内容与版本，不能同一 decision_id 换材料复用。"""
    fields = ("decision_id", "category", "question", "shared_summary", "preferences",
              "candidates", "summary_revision", "article_revision", "supplemental_evidence")
    return hashlib.sha256(json.dumps({k: request.get(k) for k in fields},
        ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def apply_settings(settings, operation):
    """宿主应只传递真实用户设置；模型输出不得调用此入口冒充授权。

    每次操作必须给 user_event_id，追溯显示给用户的选择或原话。
    返回新对象，调用方在自己的状态锁内持久化与同步，不修改输入。
    """
    result = deepcopy(settings)
    event = _text(operation, "user_event_id")
    action = _text(operation, "action")
    if action in {"preference", "correct_preference"}:
        item = {k: _text(operation, k) for k in ("id", "text", "source_ref", "scope")}
        item.update(source="user", user_event_id=event, active=True)
        if any(p["id"] == item["id"] for p in result["preferences"]):
            raise ValueError("偏好标识重复")
        if action == "correct_preference":
            old_id = _text(operation, "supersedes")
            old = next((p for p in result["preferences"] if p["id"] == old_id and p["active"]), None)
            if old is None:
                raise ValueError("被纠正偏好不存在或已经失效")
            old["active"] = False
            item["supersedes"] = old_id
        result["preferences"].append(item)
        # 偏好变化后，旧材料授权和类别评估不能继续代表当前输入。
        result["authorizations"] = {}
        for policy in result["categories"].values():
            policy.update(enabled=False, evaluation_passed=False, user_approved=False)
    elif action == "authorize":
        if operation.get("provider") != "typesafe" or operation.get("granted") is not True:
            raise ValueError("需要明确 TypeSafe 材料外发授权")
        identity = _text(operation, "decision_id")
        fingerprint = _text(operation, "content_fingerprint")
        if len(fingerprint) != 64 or any(c not in "0123456789abcdef" for c in fingerprint):
            raise ValueError("材料指纹无效")
        result["authorizations"][identity] = {"provider": "typesafe", "granted": True,
            "decision_id": identity, "content_fingerprint": fingerprint, "user_event_id": event,
            "purpose": _text(operation, "purpose")}
    elif action == "revoke_authorization":
        result["authorizations"].pop(_text(operation, "decision_id"), None)
    elif action == "category":
        category = operation.get("category")
        if category not in ALLOWED_CATEGORIES:
            raise ValueError("不允许该代决类别")
        policy = deepcopy(operation.get("policy", {}))
        if not isinstance(policy, dict):
            raise ValueError("类别设置无效")
        if any(k in policy and not isinstance(policy[k], bool) for k in
               ("enabled", "shadow_allowed", "evaluation_passed", "user_approved")):
            raise ValueError("类别权限必须为布尔值")
        if policy.get("enabled"):
            if not all(policy.get(k) is True for k in ("shadow_allowed", "evaluation_passed", "user_approved")):
                raise ValueError("需旁路评估通过及用户明确启用")
            _text(policy, "evaluation_report_ref")
            _text(policy, "evaluation_plan_ref")
            for key in ("min_probability", "min_confidence"):
                value = policy.get(key)
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
                    raise ValueError("阈值必须在 0 到 1 之间")
        policy["user_event_id"] = event
        result["categories"][category] = policy
    elif action == "mode":
        mode = operation.get("mode")
        if mode not in {"off", "shadow", "enabled"}:
            raise ValueError("JEV 模式无效")
        if mode == "enabled" and not any(p.get("enabled") is True for p in result["categories"].values()):
            raise ValueError("尚无评估通过且用户启用的类别")
        result["mode"] = mode
    elif action == "revoke_decision":
        result["revocations"].append({"decision_id": _text(operation, "decision_id"),
            "reason": _text(operation, "reason"), "user_event_id": event})
        # 不可靠类别信息时保守停用全部代决；旁路研究仍可单独开放。
        for policy in result["categories"].values():
            policy.update(enabled=False, evaluation_passed=False, user_approved=False)
        if result["mode"] == "enabled":
            result["mode"] = "off"
    else:
        raise ValueError("未知 JEV 设置操作")
    result["audit"].append(deepcopy(operation))
    return result


def runtime_state(settings, topic_id, decision_id=None):
    return {"confirmed_preferences": [deepcopy(p) for p in settings["preferences"]
            if p["active"] and p["scope"] in {"global", topic_id}],
            "jev_external_authorization": deepcopy(settings["authorizations"].get(decision_id, {})),
            "jev_settings": deepcopy(settings)}


def load_settings(path):
    path = Path(path)
    if not path.exists():
        return default_settings()
    stored = json.loads(path.read_text(encoding="utf-8"))
    # 重放审计，避免直接编辑布尔开关绕过配置入口的校验。
    rebuilt = default_settings()
    for operation in stored.get("audit", []):
        rebuilt = apply_settings(rebuilt, operation)
    if rebuilt != stored:
        raise ValueError("JEV 设置与审计记录不一致")
    return rebuilt


def save_settings(path, settings):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=path.name, suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(settings, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def prediction_input(sample):
    """白名单构造预测输入；答案和预测后的纠正永不传入模型。"""
    return deepcopy({k: sample[k] for k in ("decision_id", "category", "question",
        "shared_summary", "preferences", "candidates", "summary_revision", "article_revision", "supplemental_evidence") if k in sample})


@contextmanager
def _evaluation_transaction(path):
    """采集顺序由宿主持久化；并发写入拒绝重试，不互相覆盖。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_name(path.name + ".lock")
    try:
        handle = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise ValueError("评估记录正在写入，稍后重试；异常遗留锁需确认无写入进程后处理") from exc
    try:
        os.close(handle)
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"version": 1, "sequence": 0, "records": []}
        yield data
        save_settings(path, data)
    finally:
        lock.unlink(missing_ok=True)


def collect_prediction(path, sample, model, choice, record_ref):
    """在用户作出选择前保存一个模型/规则的真实预测，不调用模型。

    record_ref 指向实际模型输出或规则执行记录。只接受未含答案的输入；
    三个预测均固定后，才允许 record_user_choice 关联真实用户决定。
    """
    if model not in {"jev", "ai_os", "rules"}:
        raise ValueError("预测来源只能为 jev、ai_os、rules")
    if any(key in sample for key in ("user_choice", "label_source", "label_sequence", "predictions", "later_correction")):
        raise ValueError("预测输入不得含用户答案、已有预测或事后纠正")
    identity = _text(sample, "decision_id")
    if sample.get("category") not in ALLOWED_CATEGORIES:
        raise ValueError("预测类别无效")
    for field in ("question", "shared_summary"):
        _text(sample, field)
    source = _text({"record_ref": record_ref}, "record_ref")
    inputs = prediction_input(sample)
    choices = {item["id"] for item in sample.get("candidates", [])} | set(RESERVED)
    if choice not in choices:
        raise ValueError("预测选择不在候选中")
    with _evaluation_transaction(path) as data:
        row = next((r for r in data["records"] if r["decision_id"] == identity), None)
        if row is None:
            row = {**inputs, "predictions": {}, "group_id": sample.get("group_id", identity)}
            data["records"].append(row)
        if prediction_input(row) != inputs or row.get("group_id") != sample.get("group_id", identity):
            raise ValueError("同一决定的输入或分组已改变")
        if row.get("user_event_id") or model in row["predictions"]:
            raise ValueError("答案已记录或预测已固化，不能覆盖或补造")
        data["sequence"] += 1
        row["predictions"][model] = {"choice": choice, "record_ref": source,
            "input_fingerprint": content_fingerprint(inputs), "sequence": data["sequence"],
            "recorded_at": datetime.now(timezone.utc).isoformat()}
        result = deepcopy(row)
    return result


def record_user_choice(path, decision_id, choice, *, user_event_id, source_ref, user_text):
    """仅供宿主提交真实用户回复，不能以模型选择或 mock 标签填充。"""
    for key, value in dict(user_event_id=user_event_id, source_ref=source_ref, user_text=user_text).items():
        _text({key: value}, key)
    with _evaluation_transaction(path) as data:
        row = next((r for r in data["records"] if r["decision_id"] == decision_id), None)
        if row is None or set(row["predictions"]) != {"jev", "ai_os", "rules"}:
            raise ValueError("需先固化三种预测再记录用户决定")
        if row.get("user_event_id"):
            raise ValueError("真实标签已固化，纠正应另记撤销而非覆盖历史")
        if any(r.get("user_event_id") == user_event_id for r in data["records"]):
            raise ValueError("同一用户决定不能重复充当不同样本标签")
        choices = {c["id"] for c in row["candidates"]} | set(RESERVED)
        if choice not in choices:
            raise ValueError("用户选择不在候选中")
        data["sequence"] += 1
        row.update(user_choice=choice, user_event_id=user_event_id, source_ref=source_ref,
            user_text=user_text, label_source="user", label_sequence=data["sequence"],
            labelled_at=datetime.now(timezone.utc).isoformat())
        result = deepcopy(row)
    return result


def validate_sample_split(discovery, validation):
    """额外拒绝同一分组或只改 decision_id 的重复输入跨集使用。"""
    identities, groups, fingerprints, user_events = set(), set(), set(), set()
    for split in (discovery, validation):
        for row in split:
            identity = _text(row, "decision_id")
            group = row.get("group_id", identity)
            inputs = prediction_input(row)
            inputs.pop("decision_id", None)
            fingerprint = hashlib.sha256(json.dumps(inputs, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            if identity in identities or group in groups or fingerprint in fingerprints:
                raise ValueError("发现集/验证集存在重复决定、同组问题或重复输入")
            event = row.get("user_event_id")
            if event and event in user_events:
                raise ValueError("发现集/验证集重复使用同一用户决定")
            if event:
                user_events.add(event)
            identities.add(identity)
            groups.add(group)
            fingerprints.add(fingerprint)


def evaluation_report(discovery, validation, revocations=()):
    """类别分别比较 JEV、AI OS、规则；报告不自动启用，不推测缺失标签。"""
    used = {row["decision_id"] for row in discovery}
    seen, groups = set(), {}
    revoked = {row["decision_id"] for row in revocations}
    for row in validation:
        identity = _text(row, "decision_id")
        if identity in used or identity in seen:
            raise ValueError("发现集/验证集重叠或验证样本重复")
        seen.add(identity)
        if row.get("category") not in ALLOWED_CATEGORIES:
            raise ValueError("验证类别无效")
        if row.get("label_source") != "user" or not row.get("user_event_id"):
            continue
        choices = {c["id"] for c in row.get("candidates", [])} | set(RESERVED)
        truth = row.get("user_choice")
        if truth not in choices:
            raise ValueError("用户标签不在候选中")
        # 整组预测先于真实选择固化，三者必须看同一份白名单输入。
        fingerprint = content_fingerprint(prediction_input(row))
        for model in ("jev", "ai_os", "rules"):
            prediction = row.get("predictions", {}).get(model, {})
            if (prediction.get("input_fingerprint") != fingerprint
                    or type(prediction.get("sequence")) is not int
                    or type(row.get("label_sequence")) is not int
                    or prediction["sequence"] >= row["label_sequence"]
                    or prediction.get("choice") not in choices):
                raise ValueError("预测缺失、输入不同或预测晚于答案")
            stats = groups.setdefault(row["category"], {}).setdefault(model,
                dict(labelled=0, agreement=0, selected=0, should_ask_but_selected=0,
                     unnecessary_ask=0, revoked=0))
            guess = prediction["choice"]
            selected = guess not in RESERVED
            stats["labelled"] += 1
            stats["agreement"] += guess == truth
            stats["selected"] += selected
            stats["should_ask_but_selected"] += selected and truth == "__ask_user__"
            stats["unnecessary_ask"] += guess == "__ask_user__" and truth not in RESERVED
            stats["revoked"] += model == "jev" and selected and identity in revoked
    for group in groups.values():
        for stats in group.values():
            for count, rate in (("agreement", "agreement_rate"), ("selected", "selection_coverage"),
                ("should_ask_but_selected", "unsafe_delegation_rate"), ("unnecessary_ask", "unnecessary_ask_rate")):
                stats[rate] = stats[count] / stats["labelled"]
            stats["revocation_rate"] = stats["revoked"] / stats["selected"] if stats["selected"] else None
    return {"categories": groups, "discovery_count": len(discovery), "validation_count": len(validation),
            "auto_enable": False, "personal_validation_passed": False}
