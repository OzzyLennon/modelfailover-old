"""真机冒烟：不 mock，真调真 provider。

三步：
  1. 健康体检 —— 真探针打所有启用的 provider
  2. 故障转移演练 —— 故意把首选指向黑洞（127.0.0.1:9 连不上），
     看网关是不是真能自动切到备用 provider 并成功返回
  3. 单发真请求 —— 确认真实链路（真 key、真模型）能跑通

用法：python3 scripts/smoke_live.py
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from mf.config import Config, Provider, load_config, resolve_api_key  # noqa: E402
from mf.gateway import create_app  # noqa: E402
from mf.health import HealthMonitor  # noqa: E402

BLACKHOLE = "http://127.0.0.1:9/v1"


def step_health(cfg: Config) -> list[dict]:
    print("\n=== 1) 健康体检（真网络） ===")
    mon = HealthMonitor(cfg.enabled_providers(), interval_seconds=cfg.health_interval_seconds)
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
                "key": "有" if key else "缺",
                "status": st.status if st else "unknown",
                "latency_ms": st.latency_ms if st else None,
                "error": (st.error if st else "")[:70],
            }
        )
        print(f"  {p.name:16} {p.model:28} key={rows[-1]['key']} "
              f"status={rows[-1]['status']:8} {rows[-1]['latency_ms']}ms {rows[-1]['error']}")
    return rows


def step_failover(cfg: Config, backup_name: str) -> bool:
    """场景 A：探针刚跑完是 healthy，真正调用时却挂了 —— 这才是最常见的故障形态。"""
    print(f"\n=== 2A) 故障转移演练（首选=黑洞且探针骗过了健康检查，备用={backup_name}） ===")
    backup = cfg.provider(backup_name)
    if backup is None:
        print("  找不到备用 provider，跳过")
        return False

    hole = Provider(
        name="blackhole",
        style="openai",
        base_url=BLACKHOLE,
        api_key_env="MF_SMOKE_DUMMY_KEY",
        model="dead-model",
        strengths=["general"],
        priority=1,
        timeout_seconds=3,
    )
    os.environ["MF_SMOKE_DUMMY_KEY"] = "dummy-key-not-used"  # 失败点应该在网络层，不是鉴权层
    drill_cfg = Config(
        providers=[hole, backup],
        max_attempts=2,
        audit_db=f"/tmp/mf_smoke_{int(time.time())}.db",
        secrets_file=cfg.secrets_file,
        openclaw_config=cfg.openclaw_config,
    )
    mon = HealthMonitor(
        drill_cfg.enabled_providers(),
        probe=lambda p: (True, "", 3),  # 假装刚刚体检通过
    )
    app = create_app(drill_cfg, monitor=mon)
    started = time.perf_counter()
    with TestClient(app) as client:
        r = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "用一句话说明你是谁"}], "max_tokens": 60},
        )
    cost = (time.perf_counter() - started) * 1000
    print(f"  HTTP {r.status_code}  耗时 {cost:.0f}ms")
    print(f"  响应头 x-mf-provider={r.headers.get('x-mf-provider')} "
          f"x-mf-attempts={r.headers.get('x-mf-attempts')}")
    ok = False
    if r.status_code == 200:
        body = r.json()
        content = body["choices"][0]["message"]["content"]
        print(f"  模型回答: {content[:120]}")
        print(f"  失败记录: {json.dumps(body['x_mf']['failed_over'], ensure_ascii=False)[:200]}")
        ok = r.headers.get("x-mf-provider") == backup_name and int(r.headers.get("x-mf-attempts", "0")) >= 2
        print(f"  ✅ 故障转移{'生效' if ok else '未生效'}：黑洞 → {backup_name}")
    else:
        print(f"  ❌ 失败: {r.text[:300]}")
    return ok


def step_health_routing(cfg: Config, backup_name: str) -> bool:
    """场景 B：首选已经知道挂了 —— 网关应该直接绕过它，不浪费一次尝试。"""
    print(f"\n=== 2B) 健康感知的路由（首选已 down，应直接走 {backup_name}） ===")
    backup = cfg.provider(backup_name)
    hole = Provider(
        name="blackhole", style="openai", base_url=BLACKHOLE, api_key_env="MF_SMOKE_DUMMY_KEY",
        model="dead-model", strengths=["general"], priority=1, timeout_seconds=3,
    )
    os.environ["MF_SMOKE_DUMMY_KEY"] = "dummy-key-not-used"
    drill_cfg = Config(
        providers=[hole, backup], max_attempts=2,
        audit_db=f"/tmp/mf_smoke_b_{int(time.time())}.db",
        secrets_file=cfg.secrets_file, openclaw_config=cfg.openclaw_config,
    )
    mon = HealthMonitor(
        drill_cfg.enabled_providers(),
        probe=lambda p: (False, "限流 HTTP 429", 5) if p.name == "blackhole" else (True, "", 5),
    )
    app = create_app(drill_cfg, monitor=mon)
    with TestClient(app) as client:
        r = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "ping"}], "max_tokens": 20},
        )
    ok = r.status_code == 200 and r.headers.get("x-mf-provider") == backup_name
    print(f"  HTTP {r.status_code}  provider={r.headers.get('x-mf-provider')} "
          f"attempts={r.headers.get('x-mf-attempts')}")
    print(f"  {'✅' if ok else '❌'} 健康状态参与了路由决策")
    return ok


def step_real_call(cfg: Config, provider_name: str) -> bool:
    print(f"\n=== 3) 单发真请求（{provider_name}） ===")
    p = cfg.provider(provider_name)
    if p is None:
        print("  provider 不存在，跳过")
        return False
    solo = Config(
        providers=[p],
        max_attempts=1,
        audit_db=f"/tmp/mf_smoke_solo_{int(time.time())}.db",
        secrets_file=cfg.secrets_file,
        openclaw_config=cfg.openclaw_config,
    )
    mon = HealthMonitor(solo.enabled_providers())
    app = create_app(solo, monitor=mon)
    with TestClient(app) as client:
        r = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "回复两个字：收到"}], "max_tokens": 20},
        )
    if r.status_code == 200:
        print(f"  ✅ HTTP 200  provider={r.headers.get('x-mf-provider')} "
              f"model={r.headers.get('x-mf-model')}")
        print(f"  回答: {r.json()['choices'][0]['message']['content'][:100]}")
        return True
    print(f"  ❌ HTTP {r.status_code}: {r.text[:300]}")
    return False


def main() -> int:
    cfg = load_config()
    rows = step_health(cfg)
    usable = [r for r in rows if r["status"] == "healthy" and r["key"] == "有"]
    if not usable:
        print("\n没有可用的真实 provider，冒烟无法继续")
        return 1
    backup = usable[0]["provider"]
    ok_failover = step_failover(cfg, backup)
    ok_health = step_health_routing(cfg, backup)
    ok_real = step_real_call(cfg, backup)
    print("\n=== 结论 ===")
    print(f"  调用时故障转移: {'✅' if ok_failover else '❌'}")
    print(f"  健康感知路由  : {'✅' if ok_health else '❌'}")
    print(f"  真实调用      : {'✅' if ok_real else '❌'}")
    return 0 if (ok_failover and ok_health and ok_real) else 1


if __name__ == "__main__":
    sys.exit(main())
