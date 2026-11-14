"""Check: Ollama Cloud key, available models, streaming latency and tool calling.

    .venv/bin/python scripts/hw/check_ollama.py                  # list models, test a default
    .venv/bin/python scripts/hw/check_ollama.py --model gpt-oss:20b
"""
import argparse
import json
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_audio import env_key  # noqa: E402

BASE = "https://ollama.com"
TOOLS = [{
    "type": "function",
    "function": {
        "name": "record_wellbeing",
        "description": "Record how the person says they feel.",
        "parameters": {"type": "object", "properties": {
            "feeling": {"type": "string", "enum": ["ok", "unsure", "not_ok"]},
            "notes": {"type": "string"}}, "required": ["feeling"]},
    },
}]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", action="append", help="model(s) to test; default: a few fast candidates")
    args = ap.parse_args()
    headers = {"Authorization": "Bearer " + env_key("OLLAMA_API_KEY")}
    with httpx.Client(base_url=BASE, headers=headers, timeout=60) as client:
        r = client.get("/api/tags")
        if r.status_code != 200:
            sys.exit(f"[FAIL] Ollama Cloud /api/tags: status {r.status_code}")
        names = sorted(m["name"] for m in r.json().get("models", []))
        print(f"[PASS] key works; {len(names)} models: {', '.join(names)}")

        candidates = args.model or [n for n in names if any(
            k in n for k in ("gpt-oss:20b", "gpt-oss:120b", "qwen3", "gemma3:12b", "ministral", "deepseek-v3.1"))][:4]
        for model in candidates:
            body = {"model": model, "stream": True, "think": False if "gpt-oss" not in model else "low",
                    "messages": [
                        {"role": "system", "content": "You are a calm voice assistant. Reply in one short sentence."},
                        {"role": "user", "content": "My hand has been shaking a bit but I feel okay."}]}
            t0 = time.perf_counter()
            first, text = None, ""
            try:
                with client.stream("POST", "/api/chat", json=body) as resp:
                    if resp.status_code != 200:
                        print(f"[FAIL] {model}: status {resp.status_code}")
                        continue
                    for raw in resp.iter_lines():
                        if not raw:
                            continue
                        chunk = json.loads(raw)
                        piece = chunk.get("message", {}).get("content", "")
                        if piece and first is None:
                            first = time.perf_counter() - t0
                        text += piece
                        if chunk.get("done"):
                            break
            except httpx.HTTPError as e:
                print(f"[FAIL] {model}: {type(e).__name__}")
                continue
            total = time.perf_counter() - t0
            tool_body = {"model": model, "stream": False, "tools": TOOLS, "messages": [
                {"role": "user", "content": "I'm feeling fine now, just a little tired. Please record that."}]}
            t1 = time.perf_counter()
            tr = client.post("/api/chat", json=tool_body)
            calls = tr.json().get("message", {}).get("tool_calls") if tr.status_code == 200 else None
            tool_s = time.perf_counter() - t1
            print(f"[{'PASS' if text else 'FAIL'}] {model}: first token {first * 1000 if first else -1:.0f} ms, "
                  f"total {total:.2f} s, reply {text.strip()[:90]!r}")
            print(f"[{'PASS' if calls else 'FAIL'}] {model} tool call ({tool_s:.2f} s): "
                  f"{[(c['function']['name'], c['function']['arguments']) for c in calls] if calls else 'none'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
