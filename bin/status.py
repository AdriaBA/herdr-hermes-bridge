#!/usr/bin/env python3
"""Diagnose the Hermes live-state bridge.

Answers the three questions a user actually has when the sidebar looks wrong:

  1. Is the bridge installed and enabled in Hermes?
  2. Which panes is it driving, and what state did it report?
  3. If nothing reports, which gate is failing?
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

BIN = os.environ.get("HERDR_BIN_PATH") or "herdr"
HERMES_HOME = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
PLUGIN = os.path.join(HERMES_HOME, "plugins", "herdr-live-state")
BRIDGE_AGENT = "hermes"  # the --agent label the bridge reports under
VALID_STATES = ("idle", "working", "blocked", "unknown")


def herdr(*args: str) -> dict | None:
    out = subprocess.run([BIN, *args], capture_output=True, text=True)
    try:
        return json.loads(out.stdout)
    except Exception:
        return None


def plugin_state() -> tuple[bool, bool]:
    installed = os.path.isfile(os.path.join(PLUGIN, "__init__.py"))
    enabled = False
    cfg = os.path.join(HERMES_HOME, "config.yaml")
    if os.path.isfile(cfg):
        try:
            import yaml

            cfg_data = yaml.safe_load(open(cfg)) or {}
            enabled = "herdr-live-state" in cfg_data.get("plugins", {}).get("enabled", [])
        except Exception:
            enabled = "herdr-live-state" in open(cfg).read()
    return installed, enabled


def main() -> int:
    installed, enabled = plugin_state()

    print("Hermes live-state bridge")
    print(f"  hermes home      {HERMES_HOME}")
    print(f"  plugin installed {'yes' if installed else 'NO'}")
    print(f"  plugin enabled   {'yes' if enabled else 'NO'}")

    if not installed:
        print("\n  fix: copy hermes/herdr-live-state/ into <hermes home>/plugins/")
    elif not enabled:
        print('\n  fix: hermes config set plugins.enabled \'["herdr-live-state"]\'')

    snap = herdr("api", "snapshot") or {}
    # session.snapshot nests everything under result.snapshot.
    inner = (snap.get("result") or {}).get("snapshot") or {}
    agents = inner.get("agents") or []
    panes = inner.get("panes") or []

    bridged = [a for a in agents if a.get("agent") == BRIDGE_AGENT]

    print(f"\n  panes seen by Herdr : {len(panes)}")
    print(f"  agents recognised   : {len(agents)}")
    print(f"  panes driven by bridge: {len(bridged)}")

    for a in bridged:
        status = a.get("agent_status", "?")
        note = "" if status in VALID_STATES else " (server-derived rendering)"
        print(f"    {a.get('pane_id')}  {status}{note}")

    unclassified = [a for a in agents if a.get("agent") != BRIDGE_AGENT]
    if unclassified:
        print("\n  other agents (state comes from Herdr's own detection):")
        for a in unclassified:
            print(f"    {a.get('pane_id')}  {a.get('agent')}  {a.get('agent_status')}")

    if installed and enabled and not bridged:
        print("\n  Bridge is ready but no pane is reporting. It only reports when")
        print("  Hermes runs inside a Herdr pane (HERDR_ENV=1). Outside Herdr it")
        print("  is a deliberate no-op.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
