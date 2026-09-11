#!/usr/bin/env python3
"""True end-to-end: a real Hermes process running inside a real Herdr pane.

The other two suites prove the plugin's logic and that Herdr accepts its reports.
Neither proves the plugin behaves under a *real* Hermes turn, because they fire the
hook callbacks by hand. This script starts an actual `hermes chat` process in a Herdr
pane, lets it call a real tool, and watches Herdr's own snapshot for the transition.

Usage: python3 tools/e2e_real_hermes.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

BIN = os.environ.get("HERDR_BIN_PATH") or "herdr"

PROMPT = (
    "Use the terminal tool to run: sleep 6. Then reply with exactly BRIDGE_E2E_DONE "
    "and nothing else."
)


def herdr(*args: str) -> dict:
    out = subprocess.run([BIN, *args], capture_output=True, text=True).stdout
    return json.loads(out)


def herdr_silent(*args: str) -> None:
    """`pane run` prints nothing; only some subcommands emit JSON."""
    subprocess.run([BIN, *args], capture_output=True, text=True)


def status(pane_id: str, agent: str = "hermes") -> str:
    snap = (herdr("api", "snapshot").get("result") or {}).get("snapshot") or {}
    for a in snap.get("agents") or []:
        if a.get("pane_id") == pane_id and a.get("agent") == agent:
            return a.get("agent_status", "?")
    for p in snap.get("panes") or []:
        if p.get("pane_id") == pane_id:
            return p.get("agent_status", "?")
    return "<gone>"


def main() -> int:
    created = herdr("workspace", "create", "--cwd", os.path.expanduser("~"),
                    "--label", "bridge-e2e", "--no-focus")
    pane = created["result"]["root_pane"]["pane_id"]
    ws = created["result"]["workspace"]["workspace_id"]
    print(f"pane {pane} (workspace {ws})")
    print(f"baseline: {status(pane)}")

    # A real Hermes CLI session, in a real Herdr pane, inheriting HERDR_* env.
    command = f"hermes chat -q {json.dumps(PROMPT)}"
    herdr_silent("pane", "run", pane, command)

    seen: list[str] = []
    got_working = False
    deadline = time.time() + 180
    while time.time() < deadline:
        s = status(pane)
        if not seen or seen[-1] != s:
            seen.append(s)
            print(f"  t+{int(180 - (deadline - time.time())):>3}s  {s}")
        if s == "working":
            got_working = True
        if got_working and s in {"idle", "done"}:
            break
        time.sleep(1.0)

    print(f"\nobserved sequence: {seen}")
    print(f"real turn reached 'working': {got_working}")
    print(f"settled back to idle/done:   {seen[-1] in {'idle', 'done'}}")

    tail = subprocess.run(
        [BIN, "pane", "read", pane, "--source", "recent-unwrapped", "--lines", "30"],
        capture_output=True, text=True).stdout
    print("\n--- pane tail ---")
    print(tail[-1200:])

    herdr("workspace", "close", ws)
    ok = got_working and seen[-1] in {"idle", "done"}
    print("\nRESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
