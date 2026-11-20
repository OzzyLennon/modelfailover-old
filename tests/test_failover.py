"""网关级故障转移测试：伪造上游，不碰网络。"""
import pytest
from fastapi.testclient import TestClient

from conftest import health_map, make_config, make_provider
from mf.gateway import create_app
from mf.health import HealthMonitor, HealthState
from mf.upstreams import UpstreamError


def build_client(providers, chat_fn, health_states=None, max_attempts=3):
    cfg = make_config(providers, max_attempts=max_attempts)
    monitor = HealthMonitor(cfg.enabled_providers())
    for p in cfg.enabled_providers():
        st = HealthState(status=(health_states or {}).get(p.name, "healthy"))
        monitor.states[p.name] = st
    app = create_app(cfg, monitor=monitor, chat_fn=chat_fn)
    return TestClient(app), cfg


def test_failover_to_second_provider():
    p1 = make_provider("p1", strengths=["general"], priority=1)
    p2 = make_provider("p2", strengths=["general"], priority=2)
    calls = []

    def fake_chat(provider, messages, **kw):
        calls.append(provider.name)
        if provider.name == "p1":
            raise UpstreamError("timeout", "模拟超时")
        return "备用模型回答", {"provider": provider.name, "model": "m2", "latency_ms": 12, "usage": {}}

    client, _ = build_client([p1, p2], fake_chat)
    r = client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "你好"}]})
    assert r.status_code == 200
    assert calls == ["p1", "p2"]
    assert r.headers["x-mf-provider"] == "p2"
    assert r.headers["x-mf-attempts"] == "2"
    assert r.json()["choices"][0]["message"]["content"] == "备用模型回答"


def test_rate_limit_triggers_failover():
    p1 = make_provider("p1", priority=1)
    p2 = make_provider("p2", priority=2)

    def fake_chat(provider, messages, **kw):
        if provider.name == "p1":
            raise UpstreamError("rate_limit", "HTTP 429 配额耗尽")
        return "ok", {"provider": provider.name, "model": "m2", "latency_ms": 5, "usage": {}}

    client, _ = build_client([p1, p2], fake_chat)
    r = client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    assert r.headers["x-mf-provider"] == "p2"


def test_all_fail_returns_503_with_attempts():
    p1 = make_provider("p1", priority=1)
    p2 = make_provider("p2", priority=2)

    def fake_chat(provider, messages, **kw):
        raise UpstreamError("server_error", "HTTP 503")

    client, _ = build_client([p1, p2], fake_chat)
    r = client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 503
    body = r.json()["error"]
    assert body["type"] == "all_providers_failed"
    assert len(body["attempts"]) == 2
    assert all(a["error"] == "server_error" for a in body["attempts"])


def test_auth_error_also_triggers_failover():
    """每个 provider 的 key 是独立的 —— A 的 401 不代表 B 也不行，必须降级。"""
    p1 = make_provider("p1", priority=1)
    p2 = make_provider("p2", priority=2)
    calls = []

    def fake_chat(provider, messages, **kw):
        calls.append(provider.name)
        if provider.name == "p1":
            raise UpstreamError("auth", "HTTP 401")
        return "ok", {"provider": provider.name, "model": "m2", "latency_ms": 5, "usage": {}}

    client, _ = build_client([p1, p2], fake_chat)
    r = client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    assert calls == ["p1", "p2"]
    assert r.headers["x-mf-provider"] == "p2"


def test_bad_request_also_falls_over():
    """参数错误理论上各家都会犯，但少一次失败就少一次断链 —— 降级优先。"""
    p1 = make_provider("p1", priority=1)
    p2 = make_provider("p2", priority=2)

    def fake_chat(provider, messages, **kw):
        if provider.name == "p1":
            raise UpstreamError("bad_request", "HTTP 400 model not found")
        return "ok", {"provider": provider.name, "model": "m2", "latency_ms": 5, "usage": {}}

    client, _ = build_client([p1, p2], fake_chat)
    r = client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    assert r.headers["x-mf-provider"] == "p2"


def test_unexpected_exception_does_not_crash_gateway():
    p1 = make_provider("p1", priority=1)
    p2 = make_provider("p2", priority=2)

    def fake_chat(provider, messages, **kw):
        if provider.name == "p1":
            raise RuntimeError("完全没预料的异常")
        return "ok", {"provider": provider.name, "model": "m2", "latency_ms": 3, "usage": {}}

    client, _ = build_client([p1, p2], fake_chat)
    r = client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    assert r.headers["x-mf-provider"] == "p2"


def test_audit_records_every_attempt():
    p1 = make_provider("p1", priority=1)
    p2 = make_provider("p2", priority=2)

    def fake_chat(provider, messages, **kw):
        if provider.name == "p1":
            raise UpstreamError("timeout", "模拟超时")
        return "ok", {"provider": provider.name, "model": "m2", "latency_ms": 3, "usage": {"total_tokens": 9}}

    client, cfg = build_client([p1, p2], fake_chat)
    client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "写个函数"}]})
    rows = client.get("/audit?limit=10").json()["rows"]
    assert len(rows) == 2
    assert rows[0]["status"] == "ok" and rows[0]["provider"] == "p2"
    assert rows[1]["status"] == "fail" and rows[1]["error_kind"] == "timeout"
    assert rows[0]["task_class"] == "code"


def test_missing_key_falls_over_to_next_provider():
    """provider 没配 key 时应该换一家，而不是直接放弃整条链路。"""
    p1 = make_provider("p1", priority=1)
    p2 = make_provider("p2", priority=2)

    def fake_chat(provider, messages, **kw):
        if provider.name == "p1":
            raise UpstreamError("no_key", "provider p1 没有可用 API key")
        return "ok", {"provider": provider.name, "model": "m2", "latency_ms": 4, "usage": {}}

    client, _ = build_client([p1, p2], fake_chat)
    r = client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    assert r.headers["x-mf-provider"] == "p2"
