# herdr-hermes-bridge

Report **Hermes Agent's real lifecycle state** into Herdr, so a Hermes pane reflects what
the agent is actually doing instead of what its terminal happens to look like.

## The accurate framing

Herdr does not simply "guess from pixels". For a pane it owns, Herdr detects the foreground
process, reads the live bottom-buffer snapshot, can match terminal-title and progress (OSC)
signals, evaluates TOML manifests, and accepts structured lifecycle reports over its socket
API. Its model is better described as *one status authority per pane*.

The useful distinction is about **authority**, not observation:

> For Hermes, the status authority is normally a screen manifest, because Herdr hosts an
> agent it does not own. Hermes *is* the agent, so it already knows the truth — including the
> parts a screen cannot show.

Two things a screen cannot show for Hermes:

- **Delegated children.** A Hermes pane can look completely idle while N subagents work.
- **Why it is blocked.** Waiting on a human approval, waiting on a child, and having nothing
  to do are all the same still terminal.

So Hermes provides reasoning and coordination; Herdr provides access to persistent terminal
sessions containing other agents and programs. This plugin makes Hermes's half of that
contract reliable.

## What it does

Pushes Hermes's own lifecycle through `pane report-agent`, which Herdr treats as **lifecycle
authority**: while this source reports, Herdr stops running screen-manifest fallback for the
same lifecycle. That removes the competing-sources problem instead of layering on top of it.

| Hermes hook | Identity used | Reported state |
|---|---|---|
| `on_session_start` / `_reset` / `_end` / `_finalize` | `session_id` | `idle` (claims authority) |
| `pre_llm_call` → `post_llm_call` | `turn_id` | `working` → settles |
| `pre_tool_call` → `post_tool_call` | `tool_call_id` | `working` → settles |
| `subagent_start` → `subagent_stop` | `child_session_id` | `working` + `"N subagents running"` |
| `pre_approval_request` → `post_approval_response` | `pattern_key` | **`blocked`** + `"waiting for your approval"` |
| process exit (`atexit`) | — | `pane release-agent` (hands authority back) |

`idle` and `working` cover everything a pane needs; `--state` accepts only
`idle \| working \| blocked \| unknown`. `done` is server-derived from seen-state and cannot
be reported — a reported `idle` surfaces as `done` until the completion has been seen, which
is Herdr working as designed.

## Install

**Hermes half** (does the reporting):

```bash
cp -R hermes/herdr-live-state ~/.hermes/plugins/
hermes config set plugins.enabled '["herdr-agent-state", "herdr-live-state"]'
hermes plugins list      # expect: herdr-live-state | enabled
```

**Herdr half** (a diagnostic action):

```bash
herdr plugin install AdriaBA/herdr-hermes-bridge    # from GitHub
# or, developing locally:
herdr plugin link /path/to/herdr-hermes-bridge
herdr plugin action invoke hermes.live-state.status
```

### Why it is a separate plugin

`herdr integration install hermes` owns `~/.hermes/plugins/herdr-agent-state/` and reports
*session identity* for native restore. That directory belongs to Herdr and is overwritten on
every integration install. This plugin owns only *lifecycle* and lives beside it. The two are
complementary: identity from Herdr's integration, liveness from this one.

## Design notes

Both of these are load-bearing, not style.

**Hooks never do I/O.** `pre_tool_call` is a bounded hook that **fails closed** in Hermes — a
callback that times out blocks the tool call. Every callback here only mutates an in-memory
set and sets an event. All I/O happens on one ordered reporting worker.

**One ordered worker owns the wire.** It assigns `--seq` at dequeue time, so the sequence
Herdr sees is monotonic in logical order even when transitions race. Herdr ignores stale
sequence numbers from the same source, so a late report can never overwrite a newer one.

**Delivery is retried; dedupe never assumes success.** The dedupe key is the last *enqueued*
state, never a state assumed delivered. A failed report is retried by the worker, and if
every attempt fails the key is re-armed so the next event re-sends even when the resolved
state has not changed — otherwise one failed push would suppress that state forever.

**Activity is tracked by identity, not counters.** `turn_id`, `tool_call_id`,
`child_session_id`, and approval `pattern_key` come straight from the hook payloads. Set
semantics make duplicate events idempotent, and a session reset drops only that session's
activity instead of erasing unrelated work.

**It is inert outside Herdr.** Every path is gated on `HERDR_ENV=1` plus a non-empty
`HERDR_PANE_ID`.

## Verification

Three tiers, because they prove different things. All run on Herdr 0.9.0.

```bash
python3 tools/test_logic.py        # logic, fake transport
python3 tools/verify.py <pane_id>  # integration, real Herdr
python3 tools/e2e_real_hermes.py   # a real Hermes process in a real Herdr pane
```

**1. Logic** (`test_logic.py`) — expected values written independently of the plugin's
internals; the transport is faked so ordering, retry and dedupe are observable. Covers
lifecycle ordering, dedupe, duplicate-child idempotence, per-session reset isolation,
`blocked` precedence, retry with reused seq, recovery after total failure, release on exit,
and the two gates.

**2. Integration** (`verify.py`) — drives the hooks and reads Herdr's own
`session.snapshot` back. Expectations are hardcoded per step; `unknown` is only accepted at
the baseline row, so a missing final report cannot pass.

**3. Real end-to-end** (`e2e_real_hermes.py`) — starts an actual `hermes chat` process in a
real Herdr pane and watches Herdr's snapshot:

```
baseline: unknown
  t+  0s  unknown
  t+  4s  idle       <- session start claimed authority
  t+  5s  working    <- real turn running a real tool call
  t+ 27s  done       <- settled back
```

with the pane tail confirming the real turn (`sleep 6` tool call → `BRIDGE_E2E_DONE`).

### Known limitations

Stated plainly rather than implied:

- **Interrupted turns.** If a `post_*` hook never fires because the process is hard-killed
  mid-turn, that identity stays in its set. Session-boundary clears and an id cap bound the
  damage, but a long session with repeated hard interrupts could hold a stale `working`.
  Releasing authority on exit covers the common case, not every case.
- **Live coverage is one turn.** Tier 3 exercises a real tool-calling turn. Real-Hermes
  *cancellation*, *nested delegation*, and *approval* flows are covered by tiers 1–2 with
  hand-driven payloads, not yet by a live process.
- **One pane, one state.** The pane reports the union across sessions in that process, which
  is the right granularity for Herdr's model but means a multiplexed Hermes process shows
  combined activity.

## License

MIT
