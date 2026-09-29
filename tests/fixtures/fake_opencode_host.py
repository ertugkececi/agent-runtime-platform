"""A fake OpenCode bridge host, driven by ``FAKE_HOST_SCENARIO``.

The adapter tests spawn this with the same stdin/stdout shape as the real host
package. It opens no socket and never reaches a model service. The first
request is read as one line; a ``code``-mode connection reads one further line
while it waits.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

HOME_VARIABLES = (
    "HOME",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_CACHE_HOME",
    "XDG_STATE_HOME",
)

OAUTH = {
    "type": "oauth",
    "attempt_id": "attempt-1",
    "url": "https://auth.example.test/codex/device",
    "instructions": "Enter code: ABCD-EFGH",
    "mode": "auto",
}


def record(value: dict) -> None:
    sys.stdout.write(json.dumps(value) + "\n")
    sys.stdout.flush()


def environment_report() -> dict:
    """What the host sees, plus a probe file whose mode proves the child umask."""
    report = {name: os.environ.get(name) for name in HOME_VARIABLES}
    home = os.environ.get("HOME")
    if home:
        report["home_mode"] = oct(os.stat(home).st_mode & 0o777)
        probe = Path(home) / "probe.txt"
        probe.write_text("probe", encoding="utf-8")
        report["probe_mode"] = oct(probe.stat().st_mode & 0o777)
    return report


def main() -> int:
    scenario = os.environ.get("FAKE_HOST_SCENARIO", "reply")
    request = sys.stdin.readline()
    capture = os.environ.get("FAKE_HOST_CAPTURE")
    if capture:
        with open(capture, "w", encoding="utf-8") as handle:
            handle.write(request)
    env_capture = os.environ.get("FAKE_HOST_ENV_CAPTURE")
    if env_capture:
        with open(env_capture, "w", encoding="utf-8") as handle:
            json.dump(environment_report(), handle)

    if scenario == "silent":
        return 3

    if scenario != "no_hello":
        protocol = int(os.environ.get("FAKE_HOST_PROTOCOL", "2"))
        record({"type": "hello", "bridge_protocol": protocol})

    if scenario in {"reply", "no_hello", "bad_version"}:
        record({"type": "result", "kind": "reply", "content": "Fake reply."})
    elif scenario == "models":
        record({
            "type": "result",
            "kind": "models",
            "models": [
                {
                    "id": "opencode/big-model",
                    "label": "Big Model",
                    "is_default": True,
                    "default_effort": "",
                    "efforts": ["low", "high"],
                },
                {
                    "id": "opencode/plain-model",
                    "label": "Plain Model",
                    "is_default": False,
                    "default_effort": "",
                    "efforts": [],
                },
            ],
        })
    elif scenario == "invalid_models":
        record({"type": "result", "kind": "models", "models": [{"id": "opencode/big-model"}]})
    elif scenario == "integrations":
        record({
            "type": "result",
            "kind": "integrations",
            "integrations": [
                {
                    "id": "openai",
                    "name": "OpenAI",
                    "connected": False,
                    "methods": [
                        {
                            "id": "chatgpt-headless",
                            "type": "oauth",
                            "label": "ChatGPT Pro/Plus (headless)",
                        }
                    ],
                }
            ],
        })
    elif scenario == "connect_auto":
        record(OAUTH)
        record({"type": "result", "kind": "connected", "integration": "openai", "method": "chatgpt-headless"})
    elif scenario == "connect_code":
        record({**OAUTH, "mode": "code", "instructions": "Paste the code"})
        line = sys.stdin.readline()
        code = json.loads(line).get("code") if line.strip() else None
        if not code:
            record({"type": "error", "kind": "request", "message": "The connection needs a code."})
            return 1
        code_capture = os.environ.get("FAKE_HOST_CODE_CAPTURE")
        if code_capture:
            with open(code_capture, "w", encoding="utf-8") as handle:
                handle.write(code)
        record({"type": "result", "kind": "connected", "integration": "openai", "method": "chatgpt-headless"})
    elif scenario == "connect_error":
        record({"type": "error", "kind": "provider", "message": "The provider connection failed."})
        return 1
    elif scenario == "connect_stop":
        record(OAUTH)
        return 3
    elif scenario == "connect_hang":
        record(OAUTH)
        time.sleep(120)
    elif scenario == "connect_bad_oauth":
        record({"type": "oauth", "attempt_id": "attempt-1", "url": "", "instructions": 4, "mode": "manual"})
    elif scenario == "handoff":
        record({
            "type": "result",
            "kind": "handoff",
            "capability": "research",
            "task": "Find the SQLite schema for queue_jobs.",
        })
    elif scenario == "events":
        # Extra fields simulate a host that tried to log arguments or results.
        record({
            "type": "event",
            "server": "files",
            "tool": "search",
            "status": "running",
            "phase": "running",
            "arguments": {"query": "sk-test-secret-value"},
            "result": "sk-test-secret-value",
        })
        record({"type": "result", "kind": "reply", "content": "Done."})
    elif scenario == "future":
        # An additive record the adapter does not know.
        record({"type": "telemetry", "value": 1})
        record({"type": "result", "kind": "reply", "content": "Done."})
    elif scenario == "two_results":
        record({"type": "result", "kind": "reply", "content": "First."})
        record({"type": "result", "kind": "reply", "content": "Second."})
    elif scenario == "result_then_error":
        record({"type": "result", "kind": "reply", "content": "Done."})
        record({"type": "error", "kind": "provider", "message": "Late failure."})
        return 1
    elif scenario == "error":
        record({"type": "error", "kind": "provider", "message": "The fake host failed the model request."})
        return 1
    elif scenario == "startup_error":
        # No hello: the real entry point writes this when it cannot start at all.
        record({
            "type": "error",
            "kind": "provider",
            "message": "The OpenCode bridge host failed to start.",
        })
        return 1
    elif scenario == "crash":
        return 3
    elif scenario == "garbage":
        sys.stdout.write("not json\n")
        sys.stdout.flush()
        return 1
    elif scenario == "hang":
        with open(os.environ["FAKE_HOST_PID"], "w", encoding="utf-8") as handle:
            handle.write(str(os.getpid()))
        time.sleep(120)
    else:
        raise SystemExit(f"unknown FAKE_HOST_SCENARIO {scenario!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
