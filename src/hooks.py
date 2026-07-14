"""
src/hooks.py

Phase 15 (specs/0015) — opt-in, FAIL-OPEN lifecycle hooks around every tool call.

Three events, each a list of user-configured shell commands (CODE_HOOKS_CONFIG):
  * PreToolUse       — runs BEFORE a tool. An explicit DENY hard-blocks ANY tool, so policy can be about
                       the EFFECT (a path, a content pattern), not the tool NAME — this is what closes the
                       "deny is only tool-scoped" hole. Tighten-only in v1: a hook cannot force-ALLOW past
                       the engine's deny rules + fence.
  * PermissionRequest— runs at the ASK tier, BEFORE the guardian: a deterministic approver that can
                       approve/deny an ask-tier call (its LLM sibling is the guardian).
  * PostToolUse      — runs AFTER a tool, gets the result. Observe-only in v1 (side effects + logging);
                       it never alters the result or control flow.

FAIL-OPEN by construction (the opposite of the guardian): a missing / crashing / slow (timeout) /
non-JSON hook is IGNORED, so a broken hook script can never brick the agent. The hard guarantees stay the
deny rules + fence + sandbox; hooks only ADD restrictions / observability. Imports only config + logsetup
+ stdlib — no import cycle with permissions.

Protocol: each hook receives the call context as JSON on stdin and returns its verdict as a single JSON
object on stdout: {"decision": "allow"|"deny"|"ask", "message": "<why>"}. No / invalid / empty output =
no opinion (proceed). A non-zero exit with valid JSON on stdout is still honored; a non-zero exit with no
usable output fails open.
"""
import json
import subprocess
from collections import namedtuple

from . import config
from .logsetup import get_logger

log = get_logger("hooks")

PreVerdict = namedtuple("PreVerdict", "decision message")   # v1: decision is always "deny" when returned
AskVerdict = namedtuple("AskVerdict", "approved reason")    # PermissionRequest approver

_TIMEOUT = 10   # default seconds per hook; a slower hook fails OPEN (never blocks the agent)


def pretool(tool, target, args, ctx):
    """Run PreToolUse hooks. Returns PreVerdict('deny', msg) if ANY hook explicitly denies, else None
    (no opinion). FAIL-OPEN; tighten-only (an 'allow'/unknown verdict is treated as no opinion)."""
    payload = _payload("PreToolUse", tool, target, args, ctx)
    for entry in _entries("PreToolUse", tool):
        d, msg = _decision(_run(entry, payload))
        if d in ("deny", "block"):
            return PreVerdict("deny", msg)
    return None


def permission_request(tool, target, ctx):
    """Run PermissionRequest hooks for an ask-tier call. Returns AskVerdict(approved, reason) on the FIRST
    explicit allow/deny, else None (no opinion -> fall through to the guardian / human). FAIL-OPEN."""
    payload = _payload("PermissionRequest", tool, target, None, ctx)
    for entry in _entries("PermissionRequest", tool):
        d, msg = _decision(_run(entry, payload))
        if d in ("allow", "approve"):
            return AskVerdict(True, msg or "allowed")
        if d in ("deny", "block"):
            return AskVerdict(False, msg or "denied")
    return None


def posttool(tool, args, result, ctx):
    """Run PostToolUse hooks (observe-only in v1: side effects + logging, NEVER alters the result or
    control flow). FAIL-OPEN and never raises."""
    payload = _payload("PostToolUse", tool, _target_of(args), args, ctx)
    payload["ok"] = bool(getattr(result, "ok", False))
    payload["result"] = str(getattr(result, "content", ""))[:2000]
    for entry in _entries("PostToolUse", tool):
        _run(entry, payload)   # output ignored; run for its side effect


# -- internals ----------------------------------------------------------------

def _entries(event, tool):
    """Config entries for `event` whose optional `tools` filter admits `tool` (empty filter = all)."""
    out = []
    for entry in (config.load_hooks_config().get(event) or []):
        if not isinstance(entry, dict) or not entry.get("command"):
            continue
        tools = entry.get("tools")
        if tools and tool not in tools:
            continue
        out.append(entry)
    return out


def _run(entry, payload):
    """Run one hook command; return its parsed JSON-object stdout, or None on ANY failure (fail-open)."""
    cmd = entry.get("command")
    try:
        proc = subprocess.run(
            cmd, shell=True, input=json.dumps(payload), capture_output=True, text=True,
            timeout=entry.get("timeout") or _TIMEOUT, cwd=payload.get("cwd") or None)
    except Exception as e:  # noqa: BLE001 - timeout / spawn failure -> fail-open
        log.warning("hook %r failed (%s) - fail-open", cmd, e)
        return None
    out = (proc.stdout or "").strip()
    if not out:
        return None
    try:
        data = json.loads(out)
    except ValueError:
        log.warning("hook %r returned non-JSON - fail-open", cmd)
        return None
    return data if isinstance(data, dict) else None


def _decision(data):
    """(decision, message) from a hook's JSON dict, lowercased; ('', '') when there's no usable verdict."""
    if not isinstance(data, dict):
        return "", ""
    return str(data.get("decision", "")).strip().lower(), str(data.get("message", "")).strip()


def _payload(event, tool, target, args, ctx):
    return {"event": event, "tool": tool, "target": str(target),
            "args": args if isinstance(args, dict) else {},
            "cwd": getattr(ctx, "cwd", None), "depth": getattr(ctx, "depth", 0)}


def _target_of(args):
    if isinstance(args, dict):
        return args.get("path") or args.get("command") or ""
    return ""
