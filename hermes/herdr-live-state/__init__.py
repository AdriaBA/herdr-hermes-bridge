"""herdr-live-state — report Hermes Agent's real lifecycle to Herdr.

Herdr gives every pane exactly one status authority. For Hermes that authority is
normally a screen-scraping manifest, because Herdr hosts agents it does not own and
can only observe what they draw. Hermes *is* the agent, so it already knows the truth,
including the parts no screen can show: that a turn is running, that a tool call is in
flight, that it is waiting on a human approval rather than on a child, and that it has
spawned N delegated children while the pane looks quiet.

This plugin pushes that truth upstream through ``pane report-agent``, which Herdr treats
as lifecycle authority: while this source reports, Herdr stops running screen-manifest
fallback for the same lifecycle.

Deliberately separate from ``herdr-agent-state`` (installed by
``herdr integration install hermes``, which owns session identity / native restore).
That directory is Herdr's and is overwritten on every integration install; this plugin
owns only lifecycle and lives beside it.

## Reporting contract

One ordered reporting worker owns the wire. Hooks never perform I/O.

* ``pre_tool_call`` is a bounded hook that FAILS CLOSED in Hermes — a callback that
  times out blocks the tool call. Every hook callback here does nothing but mutate an
  in-memory set and set an event.
* ``--seq`` is assigned in dequeue order by the single worker, so the sequence Herdr
  sees is monotonic in logical order even when transitions race. Herdr ignores stale
  sequence numbers from the same source.
* Delivery is retried inside the worker. The dedupe key is the last *enqueued* state,
  never a state assumed delivered.
* Authority is released on process exit via ``pane release-agent``, the documented
  counterpart to claiming it.

## State model

Activity is tracked per session by real identities from the hook payloads — ``turn_id``,
``tool_call_id``, ``child_session_id``, approval ``pattern_key``/``session_key`` — not by
bare counters. Set semantics make duplicate events idempotent, and a session reset drops
only that session's activity instead of erasing unrelated work. The pane reports the
union across live sessions.
"""

from __future__ import annotations

import atexit
import os
import shutil
import subprocess
import threading
import time

# HERDR_BRIDGE_VERSION=2

_SOURCE = "herdr:hermes-live"
_AGENT = "hermes"
_INTERACTIVE_PLATFORMS = {"cli", "tui", "desktop", "acp"}
_TIMEOUT = 1.5
_MAX_ATTEMPTS = 3
# A runaway id set means stop events were lost (e.g. an interrupted turn). Cap it so a
# leak shows up as a bounded wrong state rather than unbounded memory.
_MAX_IDS = 512


class _SessionState:
    """In-flight work for one Hermes session, keyed by real identities."""

    __slots__ = ("turns", "tools", "children", "approvals")

    def __init__(self) -> None:
        self.turns: set[str] = set()
        self.tools: set[str] = set()
        self.children: set[str] = set()
        self.approvals: set[str] = set()


def _spawn(argv: list[str]) -> bool:
    """Run one Herdr CLI call. Returns True only on a clean exit.

    Module-level so tests can substitute a fake and assert ordering/retry behavior.
    """
    try:
        kwargs: dict = {"timeout": _TIMEOUT, "stdout": subprocess.DEVNULL,
                        "stderr": subprocess.DEVNULL}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        done = subprocess.run(argv, check=False, **kwargs)
        return done.returncode == 0
    except Exception:
        return False


class _Bridge:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, _SessionState] = {}
        self._pending: tuple[str, str, int] | None = None
        self._wake = threading.Event()
        self._last_enqueued: tuple[str, str] | None = None
        self._seq = 0
        self._claimed = False
        self._delivering = False
        self._worker: threading.Thread | None = None

    # ---- target resolution -------------------------------------------------

    def _pane_id(self) -> str | None:
        if os.environ.get("HERDR_ENV") != "1":
            return None
        return os.environ.get("HERDR_PANE_ID", "").strip() or None

    def _bin(self) -> str | None:
        return os.environ.get("HERDR_BIN_PATH") or shutil.which("herdr")

    # ---- state -------------------------------------------------------------

    def _session(self, session_id: str) -> _SessionState:
        state = self._sessions.get(session_id)
        if state is None:
            state = self._sessions[session_id] = _SessionState()
        return state

    def resolve(self) -> tuple[str, str]:
        """Current ``(state, message)`` for the pane, from the union of live sessions."""
        blocked = working_children = working_other = 0

        def _count(target: set[str]) -> int:
            return len(target) if len(target) <= _MAX_IDS else _MAX_IDS

        for state in self._sessions.values():
            blocked += _count(state.approvals)
            working_children += _count(state.children)
            working_other += _count(state.turns) + _count(state.tools)

        if blocked:
            return "blocked", "waiting for your approval"
        if working_children:
            label = "subagent" if working_children == 1 else "subagents"
            suffix = f" (+{working_other} active)" if working_other else ""
            return "working", f"{working_children} {label} running{suffix}"
        if working_other:
            return "working", ""
        return "idle", ""

    # ---- delivery ----------------------------------------------------------

    def emit(self, force: bool = False) -> None:
        """Enqueue the current state if it differs from the last one enqueued."""
        if self._pane_id() is None or self._bin() is None:
            return
        state, message = self.resolve()
        key = (state, message)
        with self._lock:
            if not force and key == self._last_enqueued:
                return
            self._last_enqueued = key
            self._seq += 1                     # assigned once, in logical order
            self._pending = (state, message, self._seq)
            self._ensure_worker()
        self._wake.set()

    def _ensure_worker(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        self._worker = threading.Thread(target=self._run, name="herdr-live-state",
                                        daemon=True)
        self._worker.start()

    def _run(self) -> None:
        """Single ordered delivery worker: owns sequencing, coalescing and retry."""
        while True:
            self._wake.wait()
            with self._lock:
                item = self._pending
                self._pending = None
                self._wake.clear()
            if item is None:
                continue
            state, message, seq = item
            pane_id, binary = self._pane_id(), self._bin()
            if pane_id is None or binary is None:
                continue
            with self._lock:
                self._delivering = True
            try:
                argv = [
                    binary, "pane", "report-agent",
                    "--source", _SOURCE,
                    "--agent", _AGENT,
                    "--state", state,
                    "--seq", str(seq),
                ]
                if message:
                    argv += ["--message", message]
                argv.append(pane_id)

                for attempt in range(_MAX_ATTEMPTS):
                    if _spawn(argv):
                        with self._lock:
                            if state != "idle":
                                self._claimed = True
                        break
                    # A failure must not leave the state marked delivered; the worker owns
                    # the retry so the hook thread never waits on the socket.
                    if attempt < _MAX_ATTEMPTS - 1:
                        time.sleep(0.15 * (attempt + 1))
                else:
                    # Every attempt failed. Re-arm the dedupe key so the next lifecycle
                    # event re-sends even if the resolved state has not changed; otherwise
                    # a single failed push would suppress that state forever.
                    with self._lock:
                        self._last_enqueued = None
            finally:
                with self._lock:
                    self._delivering = False

    # ---- lifecycle ---------------------------------------------------------

    def release(self) -> None:
        """Hand lifecycle authority back. Runs at process exit, so it is synchronous.

        Daemon threads do not survive interpreter shutdown, which is why this does not
        go through the queued worker.
        """
        pane_id, binary = self._pane_id(), self._bin()
        with self._lock:
            claimed = self._claimed
        if not claimed or pane_id is None or binary is None:
            return
        argv = [binary, "pane", "release-agent", "--source", _SOURCE,
                "--agent", _AGENT, pane_id]
        try:
            kwargs: dict = {"timeout": 2, "stdout": subprocess.DEVNULL,
                            "stderr": subprocess.DEVNULL}
            if os.name == "nt":
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            subprocess.run(argv, check=False, **kwargs)
        except Exception:
            pass


_bridge = _Bridge()
atexit.register(_bridge.release)


# ---- hook callbacks --------------------------------------------------------
# Every callback below is O(1) set mutation plus an event set. Nothing here may block:
# `pre_tool_call` fails closed, so a slow callback would block tool execution.


def _identity(kwargs: dict, *keys: str) -> str:
    """First non-empty identity from the payload; synthesized only as a last resort."""
    for key in keys:
        value = kwargs.get(key)
        if value:
            return str(value)
    return ""


# Identity keys per activity kind, most specific first. Using the documented payload
# fields is what makes duplicate events idempotent and stops one kind's event from
# standing in for another's.
_IDENTITY_KEYS: dict[str, tuple[str, ...]] = {
    "turns": ("turn_id", "api_request_id", "task_id"),
    "tools": ("tool_call_id", "turn_id", "api_request_id", "task_id"),
    "children": ("child_session_id", "child_subagent_id", "parent_turn_id"),
}


def _ident_for(kind: str, kwargs: dict) -> str:
    return _identity(kwargs, *_IDENTITY_KEYS[kind])


def _session_key(kwargs: dict) -> str:
    return _identity(kwargs, "session_id", "parent_session_id", "session_key")


def _platform_ok(kwargs: dict) -> bool:
    """Gate on platform only when the hook actually supplied one.

    Not every hook carries ``platform``; treating a missing key as non-interactive would
    silently drop every report for those hooks.
    """
    platform = kwargs.get("platform")
    return True if platform is None else platform in _INTERACTIVE_PLATFORMS


def _open_activity(kind: str, **kwargs) -> None:
    if not _platform_ok(kwargs):
        return
    sid = _session_key(kwargs)
    ident = _ident_for(kind, kwargs)
    if not sid or not ident:
        return
    with _bridge._lock:
        getattr(_bridge._session(sid), kind).add(ident)
    _bridge.emit()


def _close_activity(kind: str, **kwargs) -> None:
    sid = _session_key(kwargs)
    ident = _ident_for(kind, kwargs)
    if not sid or not ident:
        return
    with _bridge._lock:
        state = _bridge._sessions.get(sid)
        if state is not None:
            getattr(state, kind).discard(ident)
    _bridge.emit()


def _approval_open(**kwargs) -> None:
    sid = _session_key(kwargs)
    if not sid:
        return
    # pattern_key identifies the specific approval prompt; session_key is the fallback.
    ident = _identity(kwargs, "pattern_key", "pattern_keys", "session_key")
    with _bridge._lock:
        _bridge._session(sid).approvals.add(ident or sid)
    _bridge.emit()


def _approval_close(**kwargs) -> None:
    sid = _session_key(kwargs)
    if not sid:
        return
    ident = _identity(kwargs, "pattern_key", "pattern_keys", "session_key")
    with _bridge._lock:
        state = _bridge._sessions.get(sid)
        if state is not None:
            state.approvals.discard(ident or sid)
            if not (state.approvals or state.turns or state.tools or state.children):
                _bridge._sessions.pop(sid, None)
    _bridge.emit()


def _session_reset(**kwargs) -> None:
    """Drop only this session's activity; other sessions keep theirs."""
    sid = _session_key(kwargs)
    with _bridge._lock:
        if sid:
            _bridge._sessions.pop(sid, None)
        else:
            _bridge._sessions.clear()
    _bridge.emit(force=True)


def register(ctx):
    def turn_start(**kw):
        _open_activity("turns", **kw)

    def turn_end(**kw):
        _close_activity("turns", **kw)

    def tool_start(**kw):
        _open_activity("tools", **kw)

    def tool_end(**kw):
        _close_activity("tools", **kw)

    def child_start(**kw):
        _open_activity("children", **kw)

    def child_end(**kw):
        _close_activity("children", **kw)

    # Claim authority when the session exists, and give it back when it ends.
    ctx.register_hook("on_session_start", _session_reset)
    ctx.register_hook("on_session_reset", _session_reset)
    ctx.register_hook("on_session_end", _session_reset)
    ctx.register_hook("on_session_finalize", _session_reset)

    ctx.register_hook("pre_llm_call", turn_start)
    ctx.register_hook("post_llm_call", turn_end)
    ctx.register_hook("pre_tool_call", tool_start)
    ctx.register_hook("post_tool_call", tool_end)
    ctx.register_hook("subagent_start", child_start)
    ctx.register_hook("subagent_stop", child_end)
    ctx.register_hook("pre_approval_request", _approval_open)
    ctx.register_hook("post_approval_response", _approval_close)
