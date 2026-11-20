"""网关 HTTP 接口层测试。"""
from fastapi.testclient import TestClient

from conftest import make_config, make_provider
from mf.gateway import create_app
from mf.health import HealthMonitor, HealthState


def make_client(providers):
    cfg = make_config(providers)
    monitor = HealthMonitor(cfg.enabled_providers())
    for p in cfg.enabled_providers():
        monitor.states[p.name] = HealthState(status="healthy")
    app = create_app(cfg, monitor=monitor, chat_fn=lambda *a, **k: ("ok", {"model": "m", "latency_ms": 1, "usage": {}}))
    return TestClient(app)


def test_index_and_healthz():
    client = make_client([make_provider("p1")])
    assert client.get("/").json()["service"] == "model-failover"
    h = client.get("/healthz").json()
    assert h["status"] == "ok"
    assert "p1" in h["healthy_providers"]


def test_providers_endpoint_shows_key_presence():
    client = make_client([make_provider("p1")])
    body = client.get("/providers").json()
    assert body["providers"][0]["name"] == "p1"
    assert body["providers"][0]["key_present"] is False  # 测试配置没有 key


def test_chat_requires_messages():
    client = make_client([make_provider("p1")])
    r = client.post("/v1/chat/completions", json={})
    assert r.status_code == 422 or r.status_code == 400


def test_stream_not_supported():
    client = make_client([make_provider("p1")])
    r = client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "x"}], "stream": True})
    assert r.status_code == 400


def test_chat_response_shape_is_openai_compatible():
    client = make_client([make_provider("p1")])
    r = client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "hi"}]})
    body = r.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["role"] == "assistant"
    assert body["choices"][0]["message"]["content"] == "ok"
    assert r.headers["x-mf-attempts"] == "1"


def test_stats_endpoint():
    client = make_client([make_provider("p1")])
    client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "hi"}]})
    s = client.get("/stats").json()
    assert s["total"] >= 1 and s["ok"] >= 1


def test_model_with_slash_is_not_mangled():
    """OpenAI 风格模型名自带斜杠（Pro/moonshotai/Kimi-K2.6），不能被截断。"""
    p = make_provider("sf", models=["Pro/moonshotai/Kimi-K2.6"], model="Pro/moonshotai/Kimi-K2.6")
    seen = {}

    def fake_chat(provider, messages, **kw):
        seen["model"] = kw.get("model")
        return "ok", {"model": kw.get("model"), "latency_ms": 1, "usage": {}}

    cfg = make_config([p])
    from mf.gateway import create_app
    from mf.health import HealthMonitor, HealthState
    monitor = HealthMonitor(cfg.enabled_providers())
    monitor.states["sf"] = HealthState(status="healthy")
    client = TestClient(create_app(cfg, monitor=monitor, chat_fn=fake_chat))
    r = client.post("/v1/chat/completions", json={
        "model": "Pro/moonshotai/Kimi-K2.6",
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert r.status_code == 200
    assert seen["model"] == "Pro/moonshotai/Kimi-K2.6"
    assert r.headers["x-mf-provider"] == "sf"


def test_provider_prefix_syntax_still_works():
    """provider/model 语法仍然可用（当 provider 名不是模型的一部分时）。"""
    p = make_provider("minimax", models=["MiniMax-M3.1"], model="MiniMax-M3.1")
    seen = {}

    def fake_chat(provider, messages, **kw):
        seen["model"] = kw.get("model")
        return "ok", {"model": kw.get("model"), "latency_ms": 1, "usage": {}}

    cfg = make_config([p])
    from mf.gateway import create_app
    from mf.health import HealthMonitor, HealthState
    monitor = HealthMonitor(cfg.enabled_providers())
    monitor.states["minimax"] = HealthState(status="healthy")
    client = TestClient(create_app(cfg, monitor=monitor, chat_fn=fake_chat))
    r = client.post("/v1/chat/completions", json={
        "model": "minimax/MiniMax-M3.1",
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert r.status_code == 200
    assert seen["model"] == "MiniMax-M3.1"
