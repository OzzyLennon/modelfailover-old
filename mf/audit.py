"""审计：每一次路由决策都落一条 SQLite 记录。

审计是这产品的另一半价值 —— 出事后要能回答「这次请求走了谁、为什么、耗时多少」。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS routing_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    request_id TEXT,
    task_class TEXT,
    scores_json TEXT,
    requested_model TEXT,
    provider TEXT,
    model TEXT,
    attempt INTEGER,
    status TEXT,
    error_kind TEXT,
    error_msg TEXT,
    latency_ms INTEGER,
    prompt_chars INTEGER,
    response_chars INTEGER,
    chain_json TEXT,
    usage_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_routing_ts ON routing_log(ts DESC);
"""


class AuditLog:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=10)

    def record(self, **kw: Any) -> None:
        row = {
            "ts": time.time(),
            "request_id": kw.get("request_id"),
            "task_class": kw.get("task_class"),
            "scores_json": json.dumps(kw.get("scores") or {}, ensure_ascii=False),
            "requested_model": kw.get("requested_model"),
            "provider": kw.get("provider"),
            "model": kw.get("model"),
            "attempt": kw.get("attempt"),
            "status": kw.get("status"),
            "error_kind": kw.get("error_kind"),
            "error_msg": (kw.get("error_msg") or "")[:500],
            "latency_ms": kw.get("latency_ms"),
            "prompt_chars": kw.get("prompt_chars"),
            "response_chars": kw.get("response_chars"),
            "chain_json": json.dumps(kw.get("chain") or [], ensure_ascii=False),
            "usage_json": json.dumps(kw.get("usage") or {}, ensure_ascii=False),
        }
        cols = ",".join(row.keys())
        qs = ",".join("?" for _ in row)
        with self._lock, self._connect() as conn:
            conn.execute(f"INSERT INTO routing_log ({cols}) VALUES ({qs})", list(row.values()))

    def recent(self, limit: int = 20) -> list[dict]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM routing_log ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def stats(self) -> dict:
        with self._connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM routing_log").fetchone()[0]
            ok = conn.execute(
                "SELECT COUNT(*) FROM routing_log WHERE status='ok'"
            ).fetchone()[0]
            fail = conn.execute(
                "SELECT COUNT(*) FROM routing_log WHERE status='fail'"
            ).fetchone()[0]
            by_provider = dict(
                conn.execute(
                    "SELECT provider, COUNT(*) FROM routing_log WHERE provider IS NOT NULL GROUP BY provider"
                ).fetchall()
            )
        return {
            "total": total,
            "ok": ok,
            "fail": fail,
            "failover_rate": round(fail / total, 4) if total else 0.0,
            "by_provider": by_provider,
        }
