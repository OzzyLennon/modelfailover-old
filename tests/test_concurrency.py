"""回归测试：慢上游不能冻住整个网关。

2026-11-15 真机跑出来的问题：async 端点里调了阻塞的 httpx →
一次 58 秒的长文分析期间，/healthz 和 /audit 全部无响应
（健康探针、日志查询、并发请求全被堵死）。
修法：上游调用丢进线程池（run_in_threadpool）。

⚠️ 这个测试**必须用真 uvicorn 服务器**，不能用 TestClient ——
Starlette 的 TestClient 每次请求会新起一个事件循环，
阻塞调用冻不住第二个请求 → 测试会「假绿」。
（我第一版就是用 TestClient 写的，把代码改回阻塞版它居然照样通过。）
"""
import socket
import threading
import time

import httpx
import uvicorn

from conftest import make_config, make_provider
from mf.gateway import create_app
from mf.health import HealthMonitor, HealthState


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_slow_upstream_does_not_block_health_endpoint_real_server():
    p = make_provider("slow")

    def slow_chat(provider, messages, **kw):
        time.sleep(2.0)  # 模拟长文分析
        return "ok", {"model": "m", "latency_ms": 2000, "usage": {}}

    cfg = make_config([p])
    monitor = HealthMonitor(
        cfg.enabled_providers(), probe=lambda pr: (True, "", 1)  # 不打真实网络
    )
    monitor.states["slow"] = HealthState(status="healthy")
    app = create_app(cfg, monitor=monitor, chat_fn=slow_chat)

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()

    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            if httpx.get(f"{base}/healthz", timeout=1).status_code == 200:
                break
        except Exception:
            pass
        time.sleep(0.1)
    else:
        raise AssertionError("服务没起来")

    try:
        result = {}

        def run_slow():
            try:
                result["resp"] = httpx.post(
                    f"{base}/v1/chat/completions",
                    json={"messages": [{"role": "user", "content": "hi"}]},
                    timeout=60,
                )
            except Exception as e:  # 超时也算失败，但要记下来
                result["err"] = e

        th_slow = threading.Thread(target=run_slow)
        th_slow.start()
        time.sleep(0.6)  # 让慢请求先跑起来

        started = time.perf_counter()
        h = httpx.get(f"{base}/healthz", timeout=20)
        elapsed = time.perf_counter() - started

        th_slow.join(timeout=60)
        assert h.status_code == 200, f"/healthz 状态异常 {h.status_code}"
        assert elapsed < 1.0, f"/healthz 被慢上游堵了 {elapsed:.2f}s —— 阻塞调用跑回事件循环了"
        assert "resp" in result and result["resp"].status_code == 200
    finally:
        server.should_exit = True
        th.join(timeout=10)
