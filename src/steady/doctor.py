"""`python -m steady doctor`: check everything Steady depends on, without printing secrets.

Checks: config, database, keys (status codes only), Ollama models, headphones, and
optionally the band itself (--band; only when steadyd is not running).
"""
from __future__ import annotations

import asyncio

import httpx

from .config import Config, secret
from .store import db


def _line(name: str, ok: bool | None, detail: str = "") -> bool:
    tag = {True: "PASS", False: "FAIL", None: "SKIP"}[ok]
    print(f"[{tag}] {name}: {detail}")
    return ok is not False


def run(cfg: Config, check_band: bool = False) -> int:
    results = [_line("config", True, f"band {cfg.band_number}, transport {cfg.transport}, mode {cfg.get('mode')}")]

    try:
        db.init(cfg.db_path)
        results.append(_line("database", True, str(cfg.db_path)))
    except Exception as e:  # noqa: BLE001
        results.append(_line("database", False, repr(e)))

    dg = secret("DEEPGRAM_API_KEY", required=False)
    if dg:
        try:
            r = httpx.get("https://api.deepgram.com/v1/projects", headers={"Authorization": "Token " + dg}, timeout=10)
            results.append(_line("deepgram key", r.status_code == 200, f"status {r.status_code}"))
        except httpx.HTTPError as e:
            results.append(_line("deepgram key", False, f"network: {type(e).__name__}"))
    else:
        results.append(_line("deepgram key", False, "DEEPGRAM_API_KEY not set"))

    ol = secret("OLLAMA_API_KEY", required=False)
    if ol:
        try:
            r = httpx.get("https://ollama.com/api/tags", headers={"Authorization": "Bearer " + ol}, timeout=10)
            ok = r.status_code == 200
            results.append(_line("ollama key", ok, f"status {r.status_code}"))
            if ok:
                names = {m["name"] for m in r.json().get("models", [])}
                for key in ("voice_model", "voice_model_fallback", "openclaw_model"):
                    model = cfg.get(f"models.{key}")
                    results.append(_line(f"model {key}", model in names, str(model)))
        except httpx.HTTPError as e:
            results.append(_line("ollama key", False, f"network: {type(e).__name__}"))
    else:
        results.append(_line("ollama key", False, "OLLAMA_API_KEY not set"))

    try:
        import sounddevice as sd
        want = str(cfg.get("audio.output_device", "")).lower()
        outs = [d["name"] for d in sd.query_devices() if d["max_output_channels"] > 0]
        match = next((n for n in outs if want and want in n.lower()), None)
        results.append(_line("headphones", bool(match), match or f"none matching {want!r} in {outs}"))
    except Exception as e:  # noqa: BLE001
        results.append(_line("headphones", False, repr(e)))

    for name in ("OPENCLAW_HOOK_TOKEN", "STEADY_API_TOKEN"):
        results.append(_line(name, True if secret(name, required=False) else None,
                             "set" if secret(name, required=False) else "not set yet (needed from milestone M6)"))

    if check_band:
        results.append(_line("band", *asyncio.run(_band(cfg))))
    else:
        _line("band", None, "skipped (use --band when steadyd is not running)")

    failed = sum(1 for r in results if not r)
    print(f"\n{'All checks passed' if not failed else f'{failed} check(s) failed'}")
    return 1 if failed else 0


async def _band(cfg: Config) -> tuple[bool, str]:
    from . import kitpath  # noqa: F401
    from vexo_open import Band, VexoError
    try:
        band = await Band.connect(cfg.band_number, transport=cfg.transport)
    except VexoError as e:
        return False, f"{e.code}: {e}"
    try:
        bat = await band.get_battery()
        return True, f"{band.name} fw {band.info.firmware} via {cfg.transport}, battery {bat.percent}% ({bat.state})"
    finally:
        await band.disconnect()
