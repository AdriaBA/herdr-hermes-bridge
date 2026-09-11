#!/usr/bin/env python3
"""Live integration test: does real Herdr accept our reports and reflect them?

This is the integration half. The logic half (ordering, retry, dedupe,
identity tracking) is proven with a fake transport in `test_logic.py`; this file
only proves the wire works against a real Herdr server.

Expectations below are written down independently of the plugin's internals — the
step list encodes what Herdr *should* show, not what the plugin computed.

Usage:
    herdr workspace create --cwd /tmp --label bridge-verify --no-focus
    python3 tools/verify.py <pane_id>
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time

if len(sys.argv) > 1:
    PANE = sys.argv[1]
else:
    PANE = os.environ.get("HERDR_PANE_ID", "")
if not PANE:
    sys.exit("usage: verify.py <pane_id>   (herdr workspace create --cwd /tmp)")

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.join(os.path.dirname(HERE), "hermes", "herdr-live-state", "__init__.py")
BIN = os.environ.get("HERDR_BIN_PATH") or "herdr"

os.environ["HERDR_ENV"] = "1"
os.environ["HERDR_PANE_ID"] = PANE

spec = importlib.util.spec_from_file_location("herdr_live_state", PLUGIN)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

hooks: dict[str, list] = {}


class Ctx:
    def register_hook(self, name, cb):
        hooks.setdefault(name, []).append(cb)


mod.register(Ctx())


def fire(name, **payload):
    for cb in hooks.get(name, []):
        cb(**payload)
    settle()


def settle(timeout=4.0):
    deadline = time.time() + timeout
    quiet = 0
    while time.time() < deadline:
        if mod._bridge._pending is None and not mod._bridge._delivering:
            quiet += 1
            if quiet >= 3:
                return
        else:
            quiet = 0
        time.sleep(0.02)


def herdr_agent_status() -> str:
    """Read the state Herdr reports for our pane from its own snapshot."""
    out = subprocess.run([BIN, "api", "snapshot"], capture_output=True, text=True).stdout
    snap = (json.loads(out).get("result") or {}).get("snapshot") or {}
    for agent in snap.get("agents") or []:
        if agent.get("pane_id") == PANE and agent.get("agent") == "hermes":
            return agent.get("agent_status", "?")
    for pane in snap.get("panes") or []:
        if pane.get("pane_id") == PANE:
            return pane.get("agent_status", "?")
    return "<pane not found>"


# (label, event, expected Herdr status). `None` expected = "idle or done": Herdr
# renders a reported idle as `done` until the completion has been seen. Kept explicit
# per row rather than wildcarded, so a missing final report cannot pass.
IDLE = {"idle", "done"}
STEPS: list[tuple[str, tuple[str, dict] | None, object]] = [
    ("baseline, before any report", None, "unknown"),
    ("on_session_start", ("on_session_start", dict(session_id="s1", platform="cli")), IDLE),
    ("pre_llm_call", ("pre_llm_call", dict(session_id="s1", turn_id="t1", platform="cli")), "working"),
    ("pre_tool_call", ("pre_tool_call", dict(session_id="s1", tool_call_id="c1", turn_id="t1", platform="cli")), "working"),
    ("post_tool_call", ("post_tool_call", dict(session_id="s1", tool_call_id="c1", turn_id="t1", platform="cli")), "working"),
    ("pre_approval_request", ("pre_approval_request", dict(session_id="s1", session_key="s1", pattern_key="deploy prod")), "blocked"),
    ("post_approval_response", ("post_approval_response", dict(session_id="s1", session_key="s1", pattern_key="deploy prod", choice="once")), "working"),
    ("subagent_start #1", ("subagent_start", dict(session_id="s1", parent_session_id="s1", child_session_id="k1")), "working"),
    ("subagent_start #2", ("subagent_start", dict(session_id="s1", parent_session_id="s1", child_session_id="k2")), "working"),
    ("post_llm_call (children still live)", ("post_llm_call", dict(session_id="s1", turn_id="t1", platform="cli")), "working"),
    ("subagent_stop #1", ("subagent_stop", dict(session_id="s1", parent_session_id="s1", child_session_id="k1")), "working"),
    ("subagent_stop #2", ("subagent_stop", dict(session_id="s1", parent_session_id="s1", child_session_id="k2")), IDLE),
]

print(f"pane {PANE}  plugin {PLUGIN}")
print(f"{'step':<38} {'herdr':<9} expected")
print("-" * 70)
fails = 0
for label, event, expected in STEPS:
    if event:
        fire(event[0], **event[1])
    got = herdr_agent_status()
    ok = got in expected if isinstance(expected, set) else got == expected
    if not ok:
        fails += 1
    shown = "/".join(sorted(expected)) if isinstance(expected, set) else expected
    print(f"{label:<38} {got:<9} {shown}{'' if ok else '   <-- MISMATCH'}")

print()
print("RESULT:", "FAIL" if fails else "PASS", f"({fails} mismatch(es))")
sys.exit(1 if fails else 0)
