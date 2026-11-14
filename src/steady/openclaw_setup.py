"""`python -m steady openclaw ...`: connect Steady and OpenClaw on this Mac.

    setup [--profile P] [--port N] [--model M]   tokens, skill, hooks, local config
    gateway --profile P [--port N]               run a profile's gateway in the foreground with
                                                 OLLAMA_API_KEY from .env (for a test profile)
    status [--profile P]                          what is set up

Secrets are generated with `secrets`, written to .env (mode 600) and to OpenClaw's own
config (the hook token must be there for OpenClaw to check it). Nothing is printed.
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
from pathlib import Path

import yaml

from .config import ENV_PATH, _read_env_file
from .kitpath import ROOT

LOCAL_CONFIG = ROOT / "config" / "local.yaml"
SKILL_SRC = ROOT / "openclaw" / "skills" / "steady" / "SKILL.md"


def _oc(profile: str) -> list[str]:
    exe = shutil.which("openclaw")
    if not exe:
        raise SystemExit("openclaw is not installed (npm install -g openclaw)")
    return [exe] + (["--profile", profile] if profile else [])


def _ensure_secret(name: str) -> bool:
    """Generate and append to .env if missing. Returns True if it was created."""
    if _read_env_file(ENV_PATH).get(name) or os.environ.get(name):
        return False
    with ENV_PATH.open("a", encoding="utf-8") as f:
        f.write(f"\n{name}={secrets.token_urlsafe(32)}\n")
    os.chmod(ENV_PATH, 0o600)
    return True


def _state_dir(profile: str) -> Path:
    return Path.home() / (f".openclaw-{profile}" if profile else ".openclaw")


def _workspace(profile: str) -> Path:
    try:
        out = subprocess.run(_oc(profile) + ["config", "get", "agents.defaults.workspace"], capture_output=True,
                             text=True, timeout=30)
        value = out.stdout.strip().strip('"')
        if out.returncode == 0 and value and value not in ("undefined", "null"):
            return Path(os.path.expanduser(value))
    except subprocess.TimeoutExpired:
        pass
    return _state_dir(profile) / "workspace"


def _local_update(values: dict) -> None:
    data = yaml.safe_load(LOCAL_CONFIG.read_text()) if LOCAL_CONFIG.exists() else {}
    data = data or {}
    for dotted, v in values.items():
        node = data
        *parents, leaf = dotted.split(".")
        for p in parents:
            node = node.setdefault(p, {})
        node[leaf] = v
    LOCAL_CONFIG.write_text("# Machine-specific settings written by `python -m steady openclaw setup`. Not committed.\n"
                            + yaml.safe_dump(data, sort_keys=False))


def install_skill(profile: str) -> Path:
    workspace = _workspace(profile)
    dest = workspace / "skills" / "steady"
    dest.mkdir(parents=True, exist_ok=True)
    wrapper = dest / "steadyctl"
    python = ROOT / ".venv" / "bin" / "python"
    wrapper.write_text("#!/bin/sh\n# Installed by Steady (python -m steady openclaw setup).\n"
                       f'PYTHONPATH="{ROOT / "src"}" exec "{python}" -m steady.ctl "$@"\n')
    wrapper.chmod(0o755)
    (dest / "SKILL.md").write_text(SKILL_SRC.read_text().replace("{STEADYCTL}", str(wrapper)))
    return wrapper


def lockdown(profile: str, wrapper: Path) -> None:
    """The OpenClaw agent may run exactly one program, steadyctl, and read files. Nothing else:
    no other shell commands, no file writes, no browser, no direct messaging tool."""
    path = _state_dir(profile) / "exec-approvals.json"
    data = json.loads(path.read_text()) if path.exists() else {"version": 1}
    policy = {"security": "allowlist", "ask": "off", "askFallback": "deny", "autoAllowSkills": False}
    data["defaults"] = dict(policy)
    data.setdefault("agents", {})["main"] = {**policy, "allowlist": [{"pattern": str(wrapper), "source": "steady-setup"}]}
    path.write_text(json.dumps(data, indent=2))
    os.chmod(path, 0o600)


def setup(profile: str = "", port: int | None = None, model: str | None = None, strict: bool | None = None) -> int:
    strict = bool(profile) if strict is None else strict       # a dedicated profile is locked down by default
    made = [n for n in ("STEADY_API_TOKEN", "OPENCLAW_HOOK_TOKEN") if _ensure_secret(n)]
    hook_token = _read_env_file(ENV_PATH)["OPENCLAW_HOOK_TOKEN"]
    port = port or (18789 if not profile else 19789)
    patch: dict = {"hooks": {"enabled": True, "token": hook_token, "path": "/hooks",
                             "allowRequestSessionKey": False, "allowedAgentIds": ["main"]}}
    if profile:   # a separate test gateway: its own port, loopback only
        patch["gateway"] = {"mode": "local", "bind": "loopback", "port": port,
                            "auth": {"mode": "token", "token": secrets.token_urlsafe(32)}}
        patch["agents"] = {"defaults": {"workspace": str(_state_dir(profile) / "workspace")}}
    if model:
        patch.setdefault("agents", {}).setdefault("defaults", {})["model"] = {"primary": model}
        if model.startswith("ollama/"):
            # Declare the hosted model explicitly: older OpenClaw releases do not know newer
            # Ollama Cloud model ids. "OLLAMA_API_KEY" is the name of the variable, not a key.
            mid = model.split("/", 1)[1]
            patch["models"] = {"providers": {"ollama": {
                "baseUrl": "https://ollama.com", "apiKey": "OLLAMA_API_KEY", "api": "ollama",
                "models": [{"id": mid, "name": mid, "reasoning": False, "input": ["text"],
                            "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
                            "contextWindow": 128000, "maxTokens": 8192}]}}}
    if strict:
        patch["tools"] = {"allow": ["exec", "read"], "exec": {"host": "gateway", "security": "allowlist", "ask": "off"}}
    res = subprocess.run(_oc(profile) + ["config", "patch", "--stdin"], input=json.dumps(patch), text=True,
                         capture_output=True, timeout=60)
    if res.returncode != 0:
        print("OpenClaw refused the config patch:\n" + "\n".join(
            line for line in (res.stdout + res.stderr).splitlines() if hook_token not in line))
        return 1
    key_note = "not changed"
    if model and model.startswith("ollama/"):
        key = _read_env_file(ENV_PATH).get("OLLAMA_API_KEY")
        if key:   # the same Ollama key Steady uses, into OpenClaw's own login store (stdin, never printed)
            r = subprocess.run(_oc(profile) + ["models", "auth", "paste-api-key", "--provider", "ollama"],
                               input=key + "\n", text=True, capture_output=True, timeout=60)
            key_note = "stored in OpenClaw's login store" if r.returncode == 0 else "could NOT be stored (run `openclaw models auth paste-api-key --provider ollama`)"
    wrapper = install_skill(profile)
    if strict:
        lockdown(profile, wrapper)
    _local_update({"openclaw.hooks_url": f"http://127.0.0.1:{port}/hooks/agent", 
                   "openclaw.steadyctl": str(wrapper), **({"openclaw.model": model} if model else {})})
    print(f"OpenClaw profile: {profile or 'default'} ({_state_dir(profile)})")
    print(f"Secrets generated into .env: {', '.join(made) or 'none (already there)'}")
    print("Hooks enabled on the gateway (token from .env), loopback only.")
    if model:
        print(f"Model: {model}; Ollama key {key_note}.")
    print(f"Skill installed: {wrapper.parent}")
    print("Lockdown: " + ("ON - the agent can only run steadyctl and read files" if strict else
                          "off - the agent keeps this profile's own tool and exec policy"))
    print(f"Steady settings written: {LOCAL_CONFIG.relative_to(ROOT)}")
    print("Restart that gateway to apply: " + (f"python -m steady openclaw gateway --profile {profile}"
                                                if profile else "openclaw gateway restart"))
    return 0


def gateway(profile: str, port: int | None = None) -> int:
    """Foreground gateway with OLLAMA_API_KEY taken from .env (never printed)."""
    env = dict(os.environ)
    key = _read_env_file(ENV_PATH).get("OLLAMA_API_KEY")
    if key and not env.get("OLLAMA_API_KEY"):
        env["OLLAMA_API_KEY"] = key
    argv = _oc(profile) + ["gateway"] + (["--port", str(port)] if port else [])
    return subprocess.call(argv, env=env)


def status(profile: str = "") -> int:
    env = _read_env_file(ENV_PATH)
    local = yaml.safe_load(LOCAL_CONFIG.read_text()) if LOCAL_CONFIG.exists() else {}
    wrapper = (local or {}).get("openclaw", {}).get("steadyctl")
    print(f"STEADY_API_TOKEN: {'set' if env.get('STEADY_API_TOKEN') else 'missing'}")
    print(f"OPENCLAW_HOOK_TOKEN: {'set' if env.get('OPENCLAW_HOOK_TOKEN') else 'missing'}")
    print(f"skill + steadyctl: {wrapper if wrapper and Path(wrapper).exists() else 'not installed'}")
    print(f"hooks_url: {(local or {}).get('openclaw', {}).get('hooks_url', 'default')}")
    return 0
