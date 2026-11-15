"""Configuration (config/steady.yaml) and secrets (.env / environment).

Secrets are only ever returned to the code that needs them; nothing here prints them.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .kitpath import ROOT

CONFIG_PATH = ROOT / "config" / "steady.yaml"
ENV_PATH = ROOT / ".env"
SECRET_NAMES = ("DEEPGRAM_API_KEY", "OLLAMA_API_KEY", "OPENCLAW_HOOK_TOKEN", "STEADY_API_TOKEN")


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:]
        key, sep, value = line.partition("=")
        if sep:
            values[key.strip()] = value.strip().strip("'\"")
    return values


def secret(name: str, required: bool = True) -> str | None:
    """A key from the environment (wins) or .env. Never log the return value."""
    value = os.environ.get(name, "").strip() or _read_env_file(ENV_PATH).get(name, "").strip()
    if not value and required:
        raise SystemExit(f"{name} is not set. Put it in the environment or in {ENV_PATH.name}.")
    return value or None


@dataclass
class Config:
    raw: dict[str, Any] = field(repr=False)

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.raw
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    # Frequently used values, validated once.
    @property
    def band_number(self) -> int:
        return int(self.raw["band"]["number"])

    @property
    def transport(self) -> str:
        return str(self.raw["band"].get("transport", "ble"))

    @property
    def db_path(self) -> Path:
        p = Path(self.get("storage.db_path", "data/steady.db"))
        return p if p.is_absolute() else ROOT / p


LOCAL_PATH = ROOT / "config" / "local.yaml"      # machine-specific, not committed (written by setup commands)


def _merge(base: dict, extra: dict) -> dict:
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
    return base


def load(path: Path = CONFIG_PATH, overrides: dict[str, Any] | None = None) -> Config:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if path == CONFIG_PATH and LOCAL_PATH.exists() and not os.environ.get("STEADY_NO_LOCAL"):   # tests ignore this machine's settings
        _merge(raw, yaml.safe_load(LOCAL_PATH.read_text(encoding="utf-8")) or {})
    for dotted, value in (overrides or {}).items():
        node = raw
        *parents, leaf = dotted.split(".")
        for part in parents:
            node = node.setdefault(part, {})
        node[leaf] = value
    cfg = Config(raw)
    n = cfg.band_number
    if not 1 <= n <= 999:
        raise SystemExit(f"band.number must be 1..999, got {n}")
    if cfg.transport not in ("serial", "ble", "sim"):
        raise SystemExit(f"band.transport must be serial, ble or sim, got {cfg.transport!r}")
    return cfg
