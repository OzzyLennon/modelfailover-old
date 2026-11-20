"""命令行入口。

  python -m mf serve            启动网关（默认 127.0.0.1:8800）
  python -m mf check            健康体检：探一遍所有 provider
  python -m mf route "<提示词>"  只看路由决策，不真调
  python -m mf audit -n 20       最近审计记录
  python -m mf stats            审计统计
"""
from __future__ import annotations

import argparse
import json
import sys

from .audit import AuditLog
from .classify import classify
from .config import load_config, resolve_api_key
from .health import HealthMonitor
from .router import build_chain, explain_chain


def cmd_serve(args) -> int:
    import uvicorn

    from .gateway import create_app

    cfg = load_config(args.config)
    print(f"[mf] 监听 {cfg.host}:{cfg.port}，providers={[p.name for p in cfg.enabled_providers()]}")
    uvicorn.run(create_app(cfg), host=cfg.host, port=cfg.port, log_level=args.log_level)
    return 0


def cmd_check(args) -> int:
    cfg = load_config(args.config)
    mon = HealthMonitor(
        cfg.enabled_providers(),
        interval_seconds=cfg.health_interval_seconds,
        key_resolver=lambda p: resolve_api_key(p, cfg),
    )
    states = mon.check_all()
    rows = []
    for p in cfg.enabled_providers():
        st = states.get(p.name)
        key = resolve_api_key(p, cfg)
        rows.append(
            {
                "provider": p.name,
                "style": p.style,
                "model": p.model,
                "status": st.status if st else "unknown",
                "latency_ms": st.latency_ms if st else None,
                "key": "有" if key else "缺",
                "error": (st.error if st else "")[:80],
            }
        )
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    healthy = [r for r in rows if r["status"] == "healthy"]
    return 0 if healthy else 1


def cmd_route(args) -> int:
    cfg = load_config(args.config)
    mon = HealthMonitor(
        cfg.enabled_providers(),
        interval_seconds=cfg.health_interval_seconds,
        key_resolver=lambda p: resolve_api_key(p, cfg),
    )
    mon.check_all()
    task_class, scores = classify([{"role": "user", "content": args.prompt}])
    health = mon.snapshot()
    chain = build_chain(task_class, cfg.enabled_providers(), health, max_attempts=cfg.max_attempts)
    print(f"任务类别: {task_class}  得分: {scores}")
    for line in explain_chain(task_class, cfg.enabled_providers(), health, chain):
        print(" ", line)
    return 0


def cmd_audit(args) -> int:
    cfg = load_config(args.config)
    log = AuditLog(cfg.audit_db)
    for row in log.recent(args.n):
        print(
            f"{row['ts']:.0f} [{row['status']:4}] {row['task_class'] or '-':8} "
            f"{row['provider'] or '-':12} attempt={row['attempt']} "
            f"{row['latency_ms'] or '-'}ms {row['error_kind'] or ''} {row['error_msg'] or ''}"[:200]
        )
    return 0


def cmd_stats(args) -> int:
    cfg = load_config(args.config)
    print(json.dumps(AuditLog(cfg.audit_db).stats(), ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="mf", description="ModelFailover 网关")
    p.add_argument("-c", "--config", default=None, help="配置文件路径")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="启动网关")
    s.add_argument("--log-level", default="info")
    s.set_defaults(func=cmd_serve)

    s = sub.add_parser("check", help="健康体检")
    s.set_defaults(func=cmd_check)

    s = sub.add_parser("route", help="只看路由决策")
    s.add_argument("prompt")
    s.set_defaults(func=cmd_route)

    s = sub.add_parser("audit", help="审计记录")
    s.add_argument("-n", type=int, default=20)
    s.set_defaults(func=cmd_audit)

    s = sub.add_parser("stats", help="审计统计")
    s.set_defaults(func=cmd_stats)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
