import os
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mf.config import Config, Provider  # noqa: E402
from mf.health import HealthState  # noqa: E402


def make_provider(name="p1", **kw) -> Provider:
    base = {
        "name": name,
        "style": "openai",
        "base_url": f"https://example.invalid/{name}/v1",
        "model": f"{name}-model",
        "strengths": ["general"],
        "priority": 100,
    }
    base.update(kw)
    return Provider(**base)


def make_config(providers, **kw) -> Config:
    return Config(
        providers=list(providers),
        max_attempts=kw.pop("max_attempts", 3),
        audit_db=kw.pop("audit_db", f"/tmp/mf_test_{os.getpid()}_{uuid.uuid4().hex[:8]}.db"),
        secrets_file=kw.pop("secrets_file", "/nonexistent/secrets.env"),
        openclaw_config=kw.pop("openclaw_config", "/nonexistent/openclaw.json"),
        **kw,
    )


def health_map(states: dict[str, str]) -> dict[str, HealthState]:
    out = {}
    for name, status in states.items():
        st = HealthState(status=status)
        out[name] = st
    return out
