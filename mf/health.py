"""健康探测：每个 provider 定期探一次，状态供路由和 /healthz 使用。

状态机：unknown → healthy / degraded / down
- down: 鉴权失败（key 无效）—— 不可用，且不该再探太频繁
- degraded: 限流 / 5xx —— 还在，但优先级降低
- healthy: 探针通过
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable

import httpx

from .config import Provider
from .upstreams import UpstreamError


@dataclass
class HealthState:
    status: str = "unknown"
    last_check: float | None = None
    last_ok: float | None = None
    latency_ms: int | None = None
    error: str = ""
    consecutive_failures: int = 0

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "last_check": self.last_check,
            "last_ok": self.last_ok,
            "latency_ms": self.latency_ms,
            "error": self.error,
            "consecutive_failures": self.consecutive_failures,
        }


class HealthMonitor:
    def __init__(
        self,
        providers: list[Provider],
        probe: Callable[[Provider], tuple[bool, str, int | None]] | None = None,
        interval_seconds: int = 60,
        key_resolver: Callable[[Provider], str] | None = None,
    ):
        self.providers = providers
        self.states: dict[str, HealthState] = {p.name: HealthState() for p in providers}
        self.interval_seconds = interval_seconds
        self.key_resolver = key_resolver
        self._probe = probe or (lambda p: default_probe(p, self.key_resolver))
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

    # ---------- 探测 ----------
    def check_one(self, provider: Provider) -> HealthState:
        try:
            ok, err, latency = self._probe(provider)
        except Exception as e:  # 探针自身炸了也不能拖垮监控
            ok, err, latency = False, f"probe exception: {e}", None
        now = time.time()
        with self._lock:
            st = self.states.setdefault(provider.name, HealthState())
            st.last_check = now
            st.latency_ms = latency
            st.error = err or ""
            if ok:
                st.status = "healthy"
                st.last_ok = now
                st.consecutive_failures = 0
            else:
                st.consecutive_failures += 1
                st.status = "down" if ("鉴权" in err or "auth" in err.lower() or "key" in err.lower()) else "degraded"
        return self.states[provider.name]

    def check_all(self) -> dict[str, HealthState]:
        for p in self.providers:
            self.check_one(p)
        return self.snapshot()

    def snapshot(self) -> dict[str, HealthState]:
        with self._lock:
            return dict(self.states)

    def status_of(self, name: str) -> str:
        st = self.snapshot().get(name)
        return st.status if st else "unknown"

    # ---------- 后台线程 ----------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()

        def _loop() -> None:
            while not self._stop.is_set():
                try:
                    self.check_all()
                except Exception:
                    pass
                self._stop.wait(self.interval_seconds)

        self._thread = threading.Thread(target=_loop, name="mf-health", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()


def default_probe(
    provider: Provider, key_resolver: Callable[[Provider], str] | None = None
) -> tuple[bool, str, int | None]:
    """真探针：OpenAI 兼容打 /models，Anthropic 兼容发一个 max_tokens=1 的最小请求。

    必须带鉴权头 —— 少了会得到 401，被误判成「provider 挂了」。
    """
    if not provider.base_url:
        return False, "未配置 base_url", None
    from .config import resolve_api_key

    key = key_resolver(provider) if key_resolver else resolve_api_key(provider)
    if not key:
        return False, "没有 API key", None
    tmo = min(provider.timeout_seconds or 20.0, 15.0)
    started = time.perf_counter()
    try:
        with httpx.Client(timeout=tmo) as client:
            if provider.style == "anthropic":
                base = provider.base_url.rstrip("/")
                url = base if base.endswith("/v1") else f"{base}/v1"
                resp = client.post(
                    f"{url}/messages",
                    json={
                        "model": provider.model,
                        "max_tokens": 1,
                        "messages": [{"role": "user", "content": "ping"}],
                    },
                    headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                    timeout=tmo,
                )
            else:
                resp = client.get(
                    f"{provider.base_url.rstrip('/')}/models",
                    headers={"Authorization": f"Bearer {key}"},
                    timeout=tmo,
                )
        latency = int((time.perf_counter() - started) * 1000)
        if resp.status_code < 400:
            return True, "", latency
        if resp.status_code in (401, 403):
            return False, f"鉴权失败 HTTP {resp.status_code}", latency
        if resp.status_code == 404:
            return True, "", latency  # 服务在，只是没有这个路径
        if resp.status_code == 429:
            return False, f"限流 HTTP {resp.status_code}", latency
        return False, f"HTTP {resp.status_code}", latency
    except httpx.TimeoutException:
        return False, "探针超时", int((time.perf_counter() - started) * 1000)
    except Exception as e:
        return False, f"探针异常: {e}", int((time.perf_counter() - started) * 1000)
