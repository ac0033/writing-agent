"""额度门槛、未知拒绝、作用域隔离和密钥保护；无真实生成请求。"""
import json
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

import ai_os_connection as c


def codex_payload(used=85, secondary=20):
    return {"rateLimitsByLimitId": {"codex": {
        "primary": {"usedPercent": used, "resetsAt": time.time() + 1000},
        "secondary": {"usedPercent": secondary, "resetsAt": time.time() + 2000}}}}


def claude_payload(used=90):
    return {"observed_at": time.time(), "rate_limits": {
        "five_hour": {"used_percentage": used, "resets_at": time.time() + 1000},
        "seven_day": {"used_percentage": 20, "resets_at": time.time() + 2000}}}


@pytest.mark.parametrize("used,allowed", [(85, True), (85.001, False), (84.9, True), (100, False), (0, True)])
def test_codex_threshold(used, allowed):
    assert c.parse_codex_quota(codex_payload(used)).permits() is allowed


@pytest.mark.parametrize("used,allowed", [(90, True), (90.001, False), (89.9, True), (100, False), (0, True)])
def test_claude_threshold(used, allowed):
    assert c.parse_claude_quota(claude_payload(used)).permits() is allowed


@pytest.mark.parametrize("invalid", [None, True, "20", -1, 101, float("nan"), float("inf"), {}, []])
def test_invalid_percent_unknown(invalid):
    result = c.parse_codex_quota(codex_payload(invalid))
    assert result.remaining_percent is None
    assert not result.permits()
    assert not c.parse_claude_quota(claude_payload(invalid)).permits()


@pytest.mark.parametrize("payload", [{}, None, [], {"rateLimitsByLimitId": {}}, {"rateLimits": {}},
    {"rateLimitsByLimitId": {"codex": None}}, {"rateLimits": {"primary": "bad"}}])
def test_missing_window_unknown(payload):
    assert not c.parse_codex_quota(payload).permits()


def test_multiwindow_and_multibucket_conservative():
    payload = codex_payload(0, 95)
    assert c.parse_codex_quota(payload).remaining_percent == 5
    payload["rateLimitsByLimitId"]["other"] = {"primary": {"usedPercent": 99, "resetsAt": time.time() + 50}}
    assert c.parse_codex_quota(payload).remaining_percent == 1
    payload["rateLimits"] = codex_payload(0, 0)["rateLimitsByLimitId"]["codex"]
    assert not c.parse_codex_quota(payload).permits()


def test_old_future_and_reset_windows_refused():
    now = time.time()
    for timestamp in (now - 121, now + 1):
        assert not c.parse_codex_quota(codex_payload(), observed_at=timestamp, now=now).permits()
        payload = claude_payload()
        payload["observed_at"] = timestamp
        assert not c.parse_claude_quota(payload, now=now).permits()
    payload = codex_payload()
    payload["rateLimitsByLimitId"]["codex"]["primary"]["resetsAt"] = now - 1
    assert not c.parse_codex_quota(payload).permits()
    assert not c.QuotaStatus("codex", 90, now - 121).permits()
    assert not c.QuotaStatus("codex", 90, now, expires_at=now - 1).permits()


def test_claude_requires_both_windows():
    payload = claude_payload()
    del payload["rate_limits"]["seven_day"]
    assert not c.parse_claude_quota(payload).permits()


def manager(codex=50, claude=50):
    return c.ConnectionManager(
        lambda: c.QuotaStatus("codex", codex, time.time(), "codex result"),
        lambda: c.QuotaStatus("claude", claude, time.time(), "claude result"))


def test_default_and_fallback(monkeypatch):
    import config
    monkeypatch.setitem(config.PROVIDERS, "deepseek", {"api_key": "secret", "base_url": "https://api.deepseek.com"})
    assert manager().resolve(c.ConnectionSettings()).provider == "codex"
    assert manager(14, 10).resolve(c.ConnectionSettings()).provider == "claude"
    for remaining in (None, 0, 9.99):
        result = manager(14, remaining).resolve(c.ConnectionSettings())
        assert result.provider == "deepseek"
        assert "secret" not in repr(result)


def test_fallback_does_not_copy_codex_model_to_other_provider(monkeypatch):
    import config
    monkeypatch.setitem(config.PROVIDERS, "deepseek", {"api_key": "secret", "base_url": "https://api.deepseek.com"})
    settings = c.ConnectionSettings(model="codex-model")
    assert manager().resolve(settings).model == "codex-model"
    assert manager(0, 50).resolve(settings).model == ""
    assert manager(0, 0).resolve(settings).model == config.ROLE_MODELS["architect"][1]


def test_manual_selection_is_not_silently_overridden():
    assert manager().resolve(c.ConnectionSettings(provider="claude")).provider == "claude"
    with pytest.raises(c.ConnectionError, match="至少 10%"):
        manager(50, 9).resolve(c.ConnectionSettings(provider="claude"))
    with pytest.raises(c.ConnectionError, match="至少 15%"):
        manager(None, 50).resolve(c.ConnectionSettings(provider="codex"))


def test_refresh_each_resolve():
    values = iter([c.QuotaStatus("codex", 15, time.time()), c.QuotaStatus("codex", 14, time.time())])
    instance = c.ConnectionManager(lambda: next(values), lambda: c.QuotaStatus("claude", 20, time.time()))
    assert instance.resolve(c.ConnectionSettings()).provider == "codex"
    assert instance.resolve(c.ConnectionSettings()).provider == "claude"


def test_secret_repr_scope_and_threads():
    secret = "test-secret-never-log"
    settings = c.ConnectionSettings(provider="api", api_key=secret, model="test", base_url="https://example.com/v1")
    assert secret not in repr(settings)
    assert c.current_connection() is None
    with c.use_connection(settings):
        assert c.current_connection() is settings
        with ThreadPoolExecutor() as pool:
            assert pool.submit(c.current_connection).result() is None
        with c.use_connection(c.ConnectionSettings(provider="claude")):
            assert c.current_connection().provider == "claude"
        assert c.current_connection() is settings
    assert c.current_connection() is None


def test_bridge_writes_only_quota(tmp_path):
    payload = claude_payload()
    payload.update(api_key="SECRET", transcript_path="PRIVATE", prompt="PRIVATE")
    payload["rate_limits"]["five_hour"]["api_key"] = "SECRET"
    path = tmp_path / "quota.json"
    c.capture_claude_quota(payload, path)
    content = path.read_text()
    assert "SECRET" not in content and "PRIVATE" not in content
    assert c.probe_claude(path=path).permits()
    assert list(tmp_path.iterdir()) == [path]
    path.write_text("{invalid")
    assert not c.probe_claude(path=path).permits()


@pytest.mark.parametrize("url", ["http://example.com", "https://secret@example.com", "https://example.com?key=SECRET", "file:///secret", "https://example.com/#SECRET"])
def test_unsafe_url_rejected_without_leaking(url):
    with pytest.raises(c.ConnectionError) as exc:
        c.ConnectionSettings(provider="api", base_url=url)
    assert "SECRET" not in str(exc.value)


def test_callback_and_cli_invoke(monkeypatch):
    import agent_cli
    monkeypatch.setattr(c, "probe_codex", lambda: c.QuotaStatus("codex", 20, time.time()))
    calls = []
    monkeypatch.setattr(agent_cli, "invoke", lambda *a, **kw: calls.append((a, kw)) or agent_cli.AgentReply("result", "", "actual"))
    routes = []
    with c.use_connection(c.ConnectionSettings(), on_route=routes.append):
        assert c.invoke_connection("system", "user").text == "result"
    assert routes[0].provider == "codex"
    assert calls[0][0] == ("codex", "", "system\n\nuser")


def test_api_error_scrubbed(monkeypatch, tmp_path, capsys):
    import openai
    def bad(**kwargs):
        raise RuntimeError("SECRET PRIVATE RESPONSE")
    monkeypatch.setattr(openai, "OpenAI", bad)
    settings = c.ConnectionSettings(provider="api", api_key="SECRET", base_url="https://example.com", model="test")
    with pytest.raises(c.ConnectionError) as exc:
        c.invoke_connection("PRIVATE", "PRIVATE", settings=settings)
    assert "SECRET" not in str(exc.value) and "PRIVATE" not in str(exc.value)
    assert capsys.readouterr().out == ""
    assert list(tmp_path.iterdir()) == []


def test_capture_rejects_secret_in_numeric_field(tmp_path):
    payload = claude_payload()
    payload["rate_limits"]["five_hour"]["used_percentage"] = "SECRET"
    path = tmp_path / "quota.json"
    c.capture_claude_quota(payload, path)
    assert "SECRET" not in path.read_text()
    assert not c.probe_claude(path=path).permits()


def test_probe_only_read_methods_and_cleanup(monkeypatch):
    import io
    from types import SimpleNamespace
    sent = []
    class Input(io.StringIO):
        def write(self, text):
            sent.append(json.loads(text))
            return super().write(text)
    proc = SimpleNamespace(stdin=Input(), stdout=io.StringIO(
        json.dumps({"id": 1, "result": {}}) + "\n" +
        json.dumps({"method": "notification"}) + "\n" +
        json.dumps({"id": 2, "result": codex_payload()}) + "\n"))
    events = []
    proc.terminate = lambda: events.append("terminate")
    proc.wait = lambda timeout: events.append("wait")
    monkeypatch.setattr(c, "command_for", lambda provider: ["fake-codex"])
    monkeypatch.setattr(c.subprocess, "Popen", lambda *a, **kw: proc)
    assert c.probe_codex().permits()
    assert [v["method"] for v in sent] == ["initialize", "initialized", "account/rateLimits/read"]
    assert events == ["terminate", "wait"]
    assert proc.stdin.closed and proc.stdout.closed


def test_api_success_and_no_automatic_retry(monkeypatch):
    import openai
    from types import SimpleNamespace
    captured = {}
    class Client:
        def __init__(self, **kw):
            captured.update(kw)
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def create(self, **kw):
            captured["request"] = kw
            return SimpleNamespace(model="reported", choices=[SimpleNamespace(message=SimpleNamespace(content="answer"))])
    monkeypatch.setattr(openai, "OpenAI", Client)
    settings = c.ConnectionSettings(provider="api", api_key="SECRET", base_url="https://example.com/v1", model="model")
    reply = c.invoke_connection("system", "user", settings=settings)
    assert reply.text == "answer" and reply.actual_model == "reported"
    assert captured["max_retries"] == 0
    assert captured["request"]["messages"][-1] == {"role": "user", "content": "user"}


def test_api_budget_stops_before_sdk(monkeypatch):
    from service.model_budget import budget_observer, BudgetExceeded
    import openai
    def reject(*args):
        raise BudgetExceeded("预算耗尽")
    token = budget_observer.set(reject)
    monkeypatch.setattr(openai, "OpenAI", lambda **kw: pytest.fail("预算耗尽时不应创建API客户端"))
    try:
        with pytest.raises(BudgetExceeded, match="预算耗尽"):
            c.invoke_connection("system", "user", settings=c.ConnectionSettings(
                provider="api", api_key="SECRET", base_url="https://example.com", model="test"))
    finally:
        budget_observer.reset(token)


def test_statusline_capture_cli_reads_utf8_paths_and_prints_utf8(tmp_path):
    # 真实 statusline JSON 为 UTF-8 且含中文会话路径；Windows 本地编码不是 UTF-8。
    import subprocess, sys
    target = tmp_path / "quota.json"
    payload = {"transcript_path": "D:/私有目录/会话.jsonl", "rate_limits": {
        "five_hour": {"used_percentage": 1, "resets_at": 1999999999},
        "seven_day": {"used_percentage": 2, "resets_at": 1999999999}}}
    done = subprocess.run([sys.executable, c.__file__, "--capture-claude-quota", str(target)],
        input=json.dumps(payload, ensure_ascii=False).encode("utf-8"), capture_output=True, timeout=30,
        stdin=None)
    assert done.returncode == 0 and done.stdout.decode("utf-8") == "AI OS 额度已同步"
    saved = json.loads(target.read_text(encoding="utf-8"))
    assert set(saved) == {"observed_at", "source", "rate_limits"} and "软件安装" not in target.read_text(encoding="utf-8")
