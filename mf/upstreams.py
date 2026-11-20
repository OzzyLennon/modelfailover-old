"""上游适配：OpenAI 兼容 + Anthropic 兼容两类 provider，统一成 (content, meta)。

所有失败都归一成 UpstreamError(kind, status) —— kind 决定「要不要继续降级」。
"""
from __future__ import annotations

import json
import time
from typing import Any

import httpx

from .config import Provider


class UpstreamError(Exception):
    def __init__(self, kind: str, message: str, status: int | None = None):
        super().__init__(message)
        self.kind = kind  # timeout|rate_limit|server_error|auth|bad_request|network|parse
        self.status = status
        self.message = message

    @property
    def retryable(self) -> bool:
        # 多 provider 场景下，每个 provider 的 key/配额/网络都是独立的：
        # A 的 401（鉴权）、429（限流）、超时，都不代表 B 也不能用 —— 一律降级。
        # 唯一例外是 parse（我们自己解析不了），但继续试也无妨，成本极低。
        return True


def _classify_status(status: int, body: str) -> UpstreamError:
    low = (body or "").lower()
    if status in (401, 403):
        return UpstreamError("auth", f"HTTP {status} 鉴权失败", status)
    if status == 429:
        return UpstreamError("rate_limit", f"HTTP {status} 限流/配额耗尽", status)
    if status >= 500:
        return UpstreamError("server_error", f"HTTP {status} 服务端错误", status)
    if status == 404:
        return UpstreamError("bad_request", "HTTP 404 模型或路径不存在", status)
    return UpstreamError("bad_request", f"HTTP {status} {body[:160]}", status)


def _chat_url(provider: Provider) -> str:
    base = provider.base_url.rstrip("/")
    if provider.style == "anthropic":
        return base if base.endswith("/v1") else f"{base}/v1"
    return base


def build_messages(messages: Any) -> tuple[list[dict[str, Any]], str | None]:
    """把 OpenAI 风格的 messages 列表拆成 (messages, system)。"""
    system = None
    msgs: list[dict[str, Any]] = []
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        role = m.get("role", "user")
        content = m.get("content")
        if isinstance(content, list):
            content = " ".join(
                str(b.get("text", "")) for b in content if isinstance(b, dict)
            )
        if not isinstance(content, str):
            content = ""
        if role == "system":
            system = (system + "\n\n" + content) if system else content
            continue
        if role == "assistant":
            msgs.append({"role": "assistant", "content": content})
        else:
            msgs.append({"role": "user", "content": content})
    return msgs, system


def _timeout_cfg(tmo: float) -> httpx.Timeout:
    """连接要快失败（死上游立刻暴露），读响应要等够（长上下文分析很慢）。

    一刀切用总超时会把「模型还在算」误判成「上游挂了」→ 白白降级。
    """
    return httpx.Timeout(
        connect=min(10.0, tmo),
        read=tmo,
        write=min(60.0, tmo),
        pool=5.0,
    )


def chat(
    provider: Provider,
    messages: list[dict[str, Any]],
    *,
    api_key: str,
    model: str | None = None,
    system: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    timeout: float | None = None,
    client: httpx.Client | None = None,
) -> tuple[str, dict[str, Any]]:
    """调用上游；成功返回 (文本内容, meta)。失败抛 UpstreamError。"""
    model = model or provider.model
    if not model:
        raise UpstreamError("bad_request", f"provider {provider.name} 未配置模型")
    if not api_key:
        raise UpstreamError("no_key", f"provider {provider.name} 没有可用 API key")

    tmo = timeout or provider.timeout_seconds or 60.0
    if provider.style == "anthropic":
        url = f"{_chat_url(provider)}/messages"
        body: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens or 2000,
            "messages": messages or [{"role": "user", "content": "ping"}],
        }
        if system:
            body["system"] = system
        if temperature is not None:
            body["temperature"] = temperature
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
    else:
        url = f"{_chat_url(provider)}/chat/completions"
        full = ([{"role": "system", "content": system}] if system else []) + list(messages)
        body = {
            "model": model,
            "messages": full,
            "max_tokens": max_tokens or 2000,
        }
        if temperature is not None:
            body["temperature"] = temperature
        headers = {
            "Authorization": f"Bearer {api_key}",
            "content-type": "application/json",
        }

    own_client = client is None
    client = client or httpx.Client(timeout=_timeout_cfg(tmo))
    started = time.perf_counter()
    try:
        resp = client.post(url, json=body, headers=headers, timeout=_timeout_cfg(tmo))
    except httpx.TimeoutException as e:
        raise UpstreamError("timeout", f"请求超时（{tmo}s）: {e}") from e
    except httpx.HTTPError as e:
        raise UpstreamError("network", f"网络错误: {e}") from e
    finally:
        if own_client:
            client.close()

    latency_ms = int((time.perf_counter() - started) * 1000)
    text = resp.text or ""
    if resp.status_code >= 400:
        raise _classify_status(resp.status_code, text)

    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise UpstreamError("parse", f"响应不是 JSON: {text[:160]}") from e

    if provider.style == "anthropic":
        blocks = data.get("content") or []
        content = "".join(b.get("text", "") for b in blocks if isinstance(b, dict))
    else:
        choices = data.get("choices") or []
        if not choices:
            raise UpstreamError("parse", f"响应没有 choices: {text[:160]}")
        content = ((choices[0].get("message") or {}).get("content")) or ""
    if not isinstance(content, str):
        content = str(content)

    meta = {
        "provider": provider.name,
        "model": model,
        "latency_ms": latency_ms,
        "usage": data.get("usage") or {},
        "status": resp.status_code,
    }
    return content, meta
