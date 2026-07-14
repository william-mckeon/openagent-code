# 0015 — hooks (opt-in, fail-open lifecycle scripts)

## Goal
Let the operator inject policy/automation the built-in rules can't express, without editing the agent's
code — the deferred "pass 2" of the permission engine ([specs/0001](0001-permissions.md) built the Core
with a single `decide(tool, target, ctx)` seam so hooks slot in later). Three lifecycle events, each a
list of user-configured shell commands (`CODE_HOOKS_CONFIG`):

- **PreToolUse** — runs BEFORE a tool. An explicit `deny` **hard-blocks ANY tool**, so a policy can be
  about the *effect* (a path, a content pattern), not the tool *name*. This is what finally closes
  **"deny is only tool-scoped"** ([[permission-deny-tool-scoped]]): a static `run_command(rm:*)` deny is
  routed around via `write_file`; a PreToolUse hook sees every tool and can refuse on the effect.
- **PermissionRequest** — runs at the ASK tier, BEFORE the guardian: a *deterministic* approver that can
  approve/deny an ask-tier call (the guardian is its LLM sibling).
- **PostToolUse** — runs AFTER a tool, gets the result. Observe-only in v1 (side effects / telemetry /
  trajectory annotation for the flywheel); it never alters the result or control flow.

**FAIL-OPEN by construction** — the opposite of the guardian. A missing / crashing / slow (timeout) /
non-JSON / no-output hook is IGNORED, so a broken hook script can never brick the agent. Hooks only ADD
restrictions + observability; the hard guarantees remain the deny rules + fence + sandbox. A PreToolUse
hook is **tighten-only**: it can `deny`, it can never force-`allow` past a deny rule or the fence.

Protocol: a hook receives the call context as JSON on stdin and returns one JSON object on stdout —
`{"decision": "allow"|"deny"|"ask", "message": "<why>"}`. No / invalid / empty output = no opinion.

## Acceptance
- `src/config.py`: `CODE_HOOKS` (default false) + `CODE_HOOKS_CONFIG` (a path) + `load_hooks_config()`
  (mirrors `load_permission_rules` — missing/bad file -> no hooks, never raises).
- `src/hooks.py`: `pretool(...)` -> `PreVerdict('deny', msg)` or None (tighten-only); `permission_request(...)`
  -> `AskVerdict(approved, reason)` or None; `posttool(...)` -> None (observe-only). Each runs its event's
  commands via a bounded-timeout subprocess, honors an optional per-entry `tools` filter, and FAILS OPEN
  on any error. Imports only config + logsetup + stdlib.
- `src/permissions.py`: a PreToolUse `deny` at the TOP of `decide()` short-circuits every tool (fires at
  ALL depths — a hook is an external subprocess, no re-entrancy). The ask-tier sites consult one
  `_ask_approver` = PermissionRequest hook (headless-only, top-level) → guardian → human/block. Flag-off
  -> None everywhere, byte-identical.
- `src/agent.py`: `PostToolUse` after each tool executes (observe-only, fail-open, skipped when off).
- `scripts/check_hooks.py` proves (with real subprocess stubs): PreToolUse deny hard-blocks; a `tools`
  filter; fail-open on crash / non-JSON / timeout / no-output; allow is no-opinion (can't bypass a deny
  rule); PermissionRequest approve/deny + headless-only; PostToolUse runs but never alters the result;
  flag-off parity. Dep-free, no model, no network.

## Non-goals (v1 — noted for a later pass)
- **PreToolUse "ask" escalation** (force an otherwise-allowed call through the approver) — v1 is
  deny-or-noop.
- **PostToolUse veto / result-mutation** — v1 is observe-only (auto-verify 0014 already owns feed-back).
- **Trajectory `log_hook` records** — a PreToolUse deny already rides the existing `log_permission`; a
  dedicated hook-fire record for the flywheel is a follow-up.
- **Arg rewriting** by a hook.

## Notes
- Off by default (`CODE_HOOKS=false`), like every adoption-track phase; see `hooks.json.example`.
- The deterministic complement to the guardian (0019, LLM/fail-closed) and the static engine (0001): one
  reasons, one enforces on effect, one is the fixed floor. Together they are "operate at scale, safely."
