"""网关本体：OpenAI 兼容入口 + 健康面板 + 审计查询。

POST /v1/chat/completions
  - 自动分类任务 → 选路 → 依次尝试 → 失败自动降级
  - 响应头带 x-mf-provider / x-mf-model / x-mf-attempts / x-mf-class
  - 全部失败 → 503（带每次尝试的原因）

GET /healthz      网关自身 + 各 provider 状态
GET /providers    provider 明细
GET /audit        最近审计记录
GET /stats        审计统计（失败率、按 provider 分布）
"""
from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .audit import AuditLog
from .classify import classify
from .config import Config, Provider, load_config, resolve_api_key
from .health import HealthMonitor
from .router import build_chain, explain_chain
from .upstreams import UpstreamError, build_messages, chat


def create_app(cfg: Config | None = None, monitor: HealthMonitor | None = None,
               chat_fn=None) -> FastAPI:
    cfg = cfg or load_config()
    monitor = monitor or HealthMonitor(
        cfg.enabled_providers(),
        interval_seconds=cfg.health_interval_seconds,
        key_resolver=lambda p: resolve_api_key(p, cfg),
    )
    audit = AuditLog(cfg.audit_db)
    call = chat_fn or chat

    app = FastAPI(title="ModelFailover Gateway", version="0.1.0")
    app.state.cfg = cfg
    app.state.monitor = monitor
    app.state.audit = audit

    def _resolve_model_target(model: str | None) -> tuple[str | None, str | None]:
        """把请求里的 model 解析成 (钉住的 provider, 该 provider 用的模型名)。

        注意：OpenAI 风格模型名本身带斜杠（Pro/moonshotai/Kimi-K2.6），
        不能把第一个斜杠当成 provider 分隔符 —— 先做全量精确匹配。
        """
        if not model:
            return None, None
        for p in cfg.enabled_providers():
            if p.supports_model(model):
                return p.name, model
        if "/" in model:
            prefix, rest = model.split("/", 1)
            if cfg.provider(prefix):
                p = cfg.provider(prefix)
                if rest in p.all_models:
                    return prefix, rest
                if model in p.all_models:
                    return prefix, model
                return prefix, rest
        return None, model

    @app.on_event("startup")
    def _startup() -> None:
        monitor.check_all()
        monitor.start()

    @app.on_event("shutdown")
    def _shutdown() -> None:
        monitor.stop()

    @app.get("/")
    def index() -> dict:
        return {
            "service": "model-failover",
            "version": "0.1.0",
            "endpoints": ["/v1/chat/completions", "/healthz", "/providers", "/audit", "/stats"],
        }

    @app.get("/healthz")
    def healthz() -> dict:
        snap = monitor.snapshot()
        providers = {
            p.name: {**snap[p.name].as_dict(), "model": p.model, "base_url": p.base_url}
            if p.name in snap else {"status": "unknown"}
            for p in cfg.enabled_providers()
        }
        healthy = [n for n, s in providers.items() if s.get("status") == "healthy"]
        return {
            "status": "ok" if healthy else "degraded",
            "ts": time.time(),
            "healthy_providers": healthy,
            "providers": providers,
        }

    @app.get("/providers")
    def providers() -> dict:
        snap = monitor.snapshot()
        out = []
        for p in cfg.enabled_providers():
            st = snap.get(p.name)
            out.append(
                {
                    "name": p.name,
                    "style": p.style,
                    "base_url": p.base_url,
                    "model": p.model,
                    "models": p.all_models,
                    "strengths": p.strengths,
                    "priority": p.priority,
                    "enabled": p.enabled,
                    "key_present": bool(resolve_api_key(p, cfg)),
                    "health": st.as_dict() if st else {"status": "unknown"},
                }
            )
        return {"providers": out}

    @app.get("/audit")
    def audit_view(limit: int = 20) -> dict:
        return {"rows": audit.recent(limit)}

    @app.get("/stats")
    def stats() -> dict:
        return audit.stats()

    @app.post("/v1/chat/completions")
    async def chat_completions(
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        body: dict[str, Any]
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="请求体不是合法 JSON")
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="请求体必须是 JSON 对象")
        messages = body.get("messages")
        if not messages:
            raise HTTPException(status_code=400, detail="messages 不能为空")
        if body.get("stream"):
            raise HTTPException(status_code=400, detail="暂不支持 stream")

        local_token = cfg.local_token_env and os.getenv(cfg.local_token_env)
        if local_token and authorization not in (None, "", f"Bearer {local_token}"):
            raise HTTPException(status_code=401, detail="本地 token 不匹配")

        requested_model = body.get("model")
        task_class, scores = classify(messages)
        pinned, pinned_model = _resolve_model_target(requested_model)
        health = monitor.snapshot()
        chain = build_chain(
            task_class, cfg.enabled_providers(), health,
            max_attempts=cfg.max_attempts, pinned=pinned,
        )
        chain_desc = explain_chain(task_class, cfg.enabled_providers(), health, chain)
        request_id = uuid.uuid4().hex[:12]
        prompt_chars = sum(len(str(m.get("content", ""))) for m in messages if isinstance(m, dict))

        if not chain:
            audit.record(request_id=request_id, task_class=task_class, scores=scores,
                        requested_model=requested_model, status="fail",
                        error_kind="no_provider", error_msg="没有可用 provider",
                        chain=chain_desc, attempt=0, prompt_chars=prompt_chars)
            raise HTTPException(status_code=503, detail="没有可用 provider")

        msgs, system = build_messages(messages)
        attempts: list[dict] = []

        for idx, p in enumerate(chain, 1):
            key = resolve_api_key(p, cfg)
            model = p.model
            if pinned == p.name and pinned_model:
                model = pinned_model
            try:
                # 上游调用是阻塞的（httpx sync）—— 必须丢进线程池，
                # 否则事件循环会被冻住：一次 58s 的长文分析期间，
                # /healthz 和 /audit 全部无响应（2026-11-15 真机跑出来才发现）。
                content, meta = await run_in_threadpool(
                    call,
                    p,
                    msgs,
                    api_key=key,
                    model=model,
                    system=system,
                    temperature=body.get("temperature"),
                    max_tokens=body.get("max_tokens"),
                    timeout=body.get("timeout") or p.timeout_seconds,
                )
            except UpstreamError as e:
                attempts.append({"provider": p.name, "model": model, "error": e.kind, "msg": e.message})
                audit.record(
                    request_id=request_id, task_class=task_class, scores=scores,
                    requested_model=requested_model, provider=p.name, model=model,
                    attempt=idx, status="fail", error_kind=e.kind, error_msg=e.message,
                    chain=chain_desc, prompt_chars=prompt_chars,
                )
                # 任何上游错误都往下一家走：每个 provider 的 key/配额/网络都是独立的，
                # A 挂了不代表 B 挂了。代价是最多多花 2 次调用。
                continue
            except Exception as e:  # 兜底：任何异常都不能让网关整体挂掉
                attempts.append({"provider": p.name, "model": model, "error": "exception", "msg": str(e)})
                audit.record(
                    request_id=request_id, task_class=task_class, scores=scores,
                    requested_model=requested_model, provider=p.name, model=model,
                    attempt=idx, status="fail", error_kind="exception", error_msg=str(e),
                    chain=chain_desc, prompt_chars=prompt_chars,
                )
                continue

            audit.record(
                request_id=request_id, task_class=task_class, scores=scores,
                requested_model=requested_model, provider=p.name, model=meta.get("model", model),
                attempt=idx, status="ok", latency_ms=meta.get("latency_ms"),
                prompt_chars=prompt_chars, response_chars=len(content),
                chain=chain_desc, usage=meta.get("usage"),
            )
            payload = {
                "id": f"chatcmpl-{request_id}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": meta.get("model", model),
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "stop",
                    }
                ],
                "usage": meta.get("usage") or {},
                "x_mf": {
                    "provider": p.name,
                    "attempts": idx,
                    "task_class": task_class,
                    "chain": chain_desc,
                    "failed_over": attempts,
                },
            }
            return JSONResponse(
                content=payload,
                headers={
                    "x-mf-provider": p.name,
                    "x-mf-model": str(meta.get("model", model)),
                    "x-mf-attempts": str(idx),
                    "x-mf-class": task_class,
                    "x-mf-request-id": request_id,
                },
            )

        audit.record(
            request_id=request_id, task_class=task_class, scores=scores,
            requested_model=requested_model, status="fail", error_kind="all_failed",
            error_msg=json.dumps(attempts, ensure_ascii=False)[:500],
            chain=chain_desc, attempt=len(attempts), prompt_chars=prompt_chars,
        )
        return JSONResponse(
            status_code=503,
            content={
                "error": {
                    "type": "all_providers_failed",
                    "message": "所有上游都失败了",
                    "attempts": attempts,
                    "chain": chain_desc,
                    "request_id": request_id,
                }
            },
        )

    return app


app = None  # 由 uvicorn 工厂模式加载：uvicorn mf.gateway:create_app --factory
