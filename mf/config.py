"""配置加载 + 密钥解析。

密钥永远不在代码里，也不从命令行传 —— 解析顺序：
  1. 进程环境变量
  2. secrets.env 文件（默认 ~/.openclaw/secrets.env，或 config 里指定的 secrets_file）
  3. openclaw.json 里的 provider 配置（api_key_ref = "openclaw:<provider>"）

路径约定（2026-11-20，仓库公开前的可移植化改造）：
  - 路径里支持 `~`（按当前用户展开）与相对路径（按仓库根解析）
  - 默认值走环境变量覆盖：MF_SECRETS_FILE / MF_OPENCLAW_CONFIG / MF_AUDIT_DB
  - 原来写死 /root/.openclaw/... —— 一是暴露本机目录结构，二是换台机器即失效

每一步都只读取，不打印、不落盘。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = ROOT / "config" / "providers.yaml"
DEFAULT_SECRETS_FILE = Path(os.path.expanduser(
    os.getenv("MF_SECRETS_FILE", "~/.openclaw/secrets.env")))
DEFAULT_OPENCLAW_CONFIG = Path(os.path.expanduser(
    os.getenv("MF_OPENCLAW_CONFIG", "~/.openclaw/openclaw.json")))


@dataclass
class Provider:
    name: str
    style: str = "openai"  # openai | anthropic
    base_url: str = ""
    api_key_env: str | list[str] | None = None
    api_key_ref: str | None = None
    model: str = ""
    models: list[str] = field(default_factory=list)
    strengths: list[str] = field(default_factory=list)
    priority: int = 100
    enabled: bool = True
    timeout_seconds: float = 60.0
    notes: str = ""

    @property
    def all_models(self) -> list[str]:
        out = list(self.models)
        if self.model and self.model not in out:
            out.insert(0, self.model)
        return out

    def supports_model(self, model: str) -> bool:
        return model in self.all_models


def resolve_path(value: str, base: Path = ROOT) -> str:
    """把配置里的路径解析成绝对路径：展开 ~，相对路径按仓库根解析。"""
    p = Path(os.path.expanduser(str(value)))
    if not p.is_absolute():
        p = base / p
    return str(p)


@dataclass
class Config:
    providers: list[Provider]
    host: str = "127.0.0.1"
    port: int = 8800
    timeout_seconds: float = 60.0
    max_attempts: int = 3
    health_interval_seconds: int = 60
    audit_db: str = str(ROOT / "data" / "audit.db")
    secrets_file: str = str(DEFAULT_SECRETS_FILE)
    openclaw_config: str = str(DEFAULT_OPENCLAW_CONFIG)
    local_token_env: str | None = None

    def provider(self, name: str) -> Provider | None:
        for p in self.providers:
            if p.name == name:
                return p
        return None

    def enabled_providers(self) -> list[Provider]:
        return [p for p in self.providers if p.enabled]


def load_config(path: str | Path | None = None) -> Config:
    cfg_path = Path(path or os.getenv("MF_CONFIG") or DEFAULT_CONFIG_PATH)
    raw: dict[str, Any] = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    d = raw.get("defaults") or {}

    providers: list[Provider] = []
    for item in raw.get("providers") or []:
        providers.append(
            Provider(
                name=item["name"],
                style=item.get("style", "openai"),
                base_url=(item.get("base_url") or "").rstrip("/"),
                api_key_env=item.get("api_key_env"),
                api_key_ref=item.get("api_key_ref"),
                model=item.get("model", ""),
                models=list(item.get("models") or []),
                strengths=list(item.get("strengths") or []),
                priority=int(item.get("priority", 100)),
                enabled=bool(item.get("enabled", True)),
                timeout_seconds=float(item.get("timeout_seconds", d.get("timeout_seconds", 60))),
                notes=item.get("notes", ""),
            )
        )

    return Config(
        providers=providers,
        host=raw.get("host", "127.0.0.1"),
        port=int(raw.get("port", 8800)),
        timeout_seconds=float(d.get("timeout_seconds", 60)),
        max_attempts=int(d.get("max_attempts", 3)),
        health_interval_seconds=int(d.get("health_interval_seconds", 60)),
        audit_db=resolve_path(raw.get("audit_db") or (str(ROOT / "data" / "audit.db"))),
        secrets_file=resolve_path(raw.get("secrets_file") or str(DEFAULT_SECRETS_FILE)),
        openclaw_config=resolve_path(raw.get("openclaw_config") or str(DEFAULT_OPENCLAW_CONFIG)),
        local_token_env=raw.get("local_token_env"),
    )


def _read_secrets_file(path: str) -> dict[str, str]:
    out: dict[str, str] = {}
    p = Path(path)
    if not p.exists():
        return out
    for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:]
        k, _, v = line.partition("=")
        v = v.strip().strip('"').strip("'")
        if k.strip():
            out[k.strip()] = v
    return out


def _read_openclaw_providers(path: str) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        cfg = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return (cfg.get("models") or {}).get("providers") or {}


def resolve_api_key(provider: Provider, cfg: Config | None = None) -> str:
    """按 环境变量 → secrets.env → openclaw.json 的顺序找 key。找不到返回 ""。"""
    secrets = _read_secrets_file(cfg.secrets_file) if cfg else {}

    if provider.api_key_env:
        names = (
            provider.api_key_env
            if isinstance(provider.api_key_env, (list, tuple))
            else [provider.api_key_env]
        )
        for n in names:
            v = os.environ.get(n) or secrets.get(n)
            if v:
                return v.strip()

    ref = provider.api_key_ref or ""
    if ref.startswith("openclaw:"):
        name = ref.split(":", 1)[1]
        provs = _read_openclaw_providers(cfg.openclaw_config if cfg else str(DEFAULT_OPENCLAW_CONFIG))
        key = (provs.get(name) or {}).get("apiKey")
        if key:
            return str(key).strip()
    elif ref.startswith("secrets:"):
        v = secrets.get(ref.split(":", 1)[1])
        if v:
            return v.strip()

    return ""


def masked(value: str) -> str:
    if not value:
        return "<missing>"
    return f"{value[:4]}…{value[-2:]}(len={len(value)})"
