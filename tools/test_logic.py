#!/usr/bin/env python3
"""Logic tests for herdr-live-state.

Deliberately NOT an agreement test. Expected values are written down independently of
the plugin's internals, and the transport is faked so ordering, retry and dedupe are
observable. Payloads use the real identity keys documented for each Hermes hook
(`turn_id`, `tool_call_id`, `child_session_id`, `pattern_key`).

Run: python3 tools/test_logic.py
"""

from __future__ import annotations

import importlib.util
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.join(os.path.dirname(HERE), "hermes", "herdr-live-state", "__init__.py")

os.environ["HERDR_ENV"] = "1"
os.environ["HERDR_PANE_ID"] = "w9:p9"
os.environ["HERDR_BIN_PATH"] = "herdr"

spec = importlib.util.spec_from_file_location("herdr_live_state", PLUGIN)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

sent: list[tuple[str, str, int]] = []   # (state, message, seq) in delivery order
results: list[bool] = []                # scripted _spawn outcomes


def fake_spawn(argv):
    state = argv[argv.index("--state") + 1]
    seq = int(argv[argv.index("--seq") + 1])
    message = argv[argv.index("--message") + 1] if "--message" in argv else ""
    sent.append((state, message, seq))
    return results.pop(0) if results else True


mod._spawn = fake_spawn

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
    """Wait for the ordered worker to finish, including any retry backoff."""
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
    raise AssertionError("worker did not drain")


def reset():
    sent.clear()
    results.clear()
    mod._bridge._sessions.clear()
    mod._bridge._last_enqueued = None
    mod._bridge._seq = 0
    mod._bridge._claimed = False


FAILURES: list[str] = []


def check(label: str, got, want):
    if got != want:
        FAILURES.append(f"{label}\n      got:  {got!r}\n      want: {want!r}")
        print(f"  FAIL  {label}")
    else:
        print(f"  ok    {label}")


# --- 1. forward transitions against independent expectations ------------------
print("\n1. lifecycle transitions")
reset()
fire("on_session_start", session_id="s1", platform="cli")
fire("pre_llm_call", session_id="s1", turn_id="t1", platform="cli")
fire("pre_tool_call", session_id="s1", tool_call_id="c1", turn_id="t1", platform="cli")
fire("post_tool_call", session_id="s1", tool_call_id="c1", turn_id="t1", platform="cli")
fire("pre_approval_request", session_id="s1", session_key="s1", pattern_key="rm -rf")
fire("post_approval_response", session_id="s1", session_key="s1", pattern_key="rm -rf",
     choice="once")
fire("subagent_start", session_id="s1", parent_session_id="s1", child_session_id="k1")
fire("subagent_start", session_id="s1", parent_session_id="s1", child_session_id="k2")
fire("post_llm_call", session_id="s1", turn_id="t1", platform="cli")
fire("subagent_stop", session_id="s1", parent_session_id="s1", child_session_id="k1")
fire("subagent_stop", session_id="s1", parent_session_id="s1", child_session_id="k2")

check("delivered sequence (state, message)", [(s, m) for s, m, _ in sent],
      [("idle", ""),
       ("working", ""),
       ("blocked", "waiting for your approval"),
       ("working", ""),
       # turn t1 is still in flight while the first child runs
       ("working", "1 subagent running (+1 active)"),
       ("working", "2 subagents running (+1 active)"),
       # post_llm_call ended the turn; both children keep running. This transition is
       # the reason the bridge exists: the pane looks quiet but work continues.
       ("working", "2 subagents running"),
       ("working", "1 subagent running"),
       ("idle", "")])
check("seq strictly increasing", [q for _, _, q in sent], [1, 2, 3, 4, 5, 6, 7, 8, 9])


# --- 2. dedupe: unchanged state is not re-sent --------------------------------
print("\n2. dedupe")
reset()
fire("pre_llm_call", session_id="s1", turn_id="t1", platform="cli")
fire("pre_tool_call", session_id="s1", tool_call_id="c1", turn_id="t1", platform="cli")
fire("pre_tool_call", session_id="s1", tool_call_id="c2", turn_id="t1", platform="cli")
check("still one 'working' push for 3 events", [s for s, _, _ in sent], ["working"])


# --- 3. duplicate child events are idempotent (identity sets, not counters) ----
print("\n3. duplicate child events")
reset()
for _ in range(3):   # same child reported three times
    fire("subagent_start", session_id="s1", parent_session_id="s1", child_session_id="k1")
fire("post_llm_call", session_id="s1", turn_id="t1", platform="cli")
with mod._bridge._lock:
    n_children = len(mod._bridge._sessions["s1"].children)
check("one child tracked, not three", n_children, 1)
reset()
fire("subagent_start", session_id="s1", parent_session_id="s1", child_session_id="k1")
fire("subagent_stop", session_id="s1", parent_session_id="s1", child_session_id="k1")
check("state returns to idle after the single matching stop",
      [s for s, _, _ in sent], ["working", "idle"])


# --- 4. session reset drops only that session --------------------------------
print("\n4. per-session reset isolation")
reset()
fire("pre_llm_call", session_id="sA", turn_id="a1", platform="cli")
fire("pre_llm_call", session_id="sB", turn_id="b1", platform="cli")
fire("on_session_reset", session_id="sA", platform="cli")
with mod._bridge._lock:
    live = sorted(mod._bridge._sessions)
check("session B survives A's reset", live, ["sB"])
check("pane still reports working", mod._bridge.resolve()[0], "working")


# --- 5. blocked outranks working ---------------------------------------------
print("\n5. blocked precedence")
reset()
fire("pre_llm_call", session_id="s1", turn_id="t1", platform="cli")
fire("pre_approval_request", session_id="s1", session_key="s1", pattern_key="deploy prod")
check("approval beats an active turn", mod._bridge.resolve()[0], "blocked")
check("block message names the reason", mod._bridge.resolve()[1], "waiting for your approval")


# --- 6. retry on transient failure, same seq reused --------------------------
print("\n6. retry")
reset()
results.extend([False, False, True])
fire("pre_llm_call", session_id="s1", turn_id="t1", platform="cli")
check("three attempts made", len(sent), 3)
check("retries reuse the logical seq", sorted({q for _, _, q in sent}), [1])


# --- 7. total failure re-arms instead of suppressing forever ------------------
print("\n7. recovery after total failure")
reset()
results.extend([False, False, False])
fire("pre_llm_call", session_id="s1", turn_id="t1", platform="cli")
check("all attempts failed", len(sent), 3)
sent.clear()
fire("pre_tool_call", session_id="s1", tool_call_id="c1", turn_id="t1", platform="cli")
check("same resolved state is re-sent after failure", len(sent), 1)


# --- 8. authority released on exit -------------------------------------------
print("\n8. release on exit")
reset()
calls: list[list[str]] = []
mod._spawn = lambda argv: (calls.append(list(argv)), True)[1]
fire("pre_llm_call", session_id="s1", turn_id="t1", platform="cli")
mod._spawn = fake_spawn
released: list[list[str]] = []
real_run = mod.subprocess.run
mod.subprocess.run = lambda argv, **kw: (released.append(list(argv)), type(
    "R", (), {"returncode": 0})())[1]
mod._bridge.release()
mod.subprocess.run = real_run
check("release-agent invoked", bool(released) and "release-agent" in released[0], True)
check("release names the source", released[0][released[0].index("--source") + 1],
      "herdr:hermes-live")


# --- 9. no-op outside Herdr --------------------------------------------------
print("\n9. inert outside Herdr")
reset()
os.environ.pop("HERDR_ENV", None)
fire("pre_llm_call", session_id="s1", turn_id="t1", platform="cli")
check("nothing sent outside a Herdr pane", sent, [])
os.environ["HERDR_ENV"] = "1"


# --- 10. non-interactive platforms are ignored -------------------------------
print("\n10. platform gate")
reset()
fire("pre_llm_call", session_id="s1", turn_id="t1", platform="cron")
check("cron session not reported", sent, [])


print("\n" + "=" * 60)
if FAILURES:
    print(f"FAIL — {len(FAILURES)} problem(s):")
    for f in FAILURES:
        print("  - " + f)
    sys.exit(1)
print("PASS — all checks green")
