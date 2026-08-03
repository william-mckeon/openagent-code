# 0057 — interactive guardian (agentic permissions)

Status: implemented
Flag: `CODE_GUARDIAN_INTERACTIVE` (default off)

## Goal

Stop making the human answer every ask-tier `[y/N]` in the REPL for a safe, on-request command. A live run
prompted for a `curl http://localhost:8080/` health-check — which is exactly the probe the runtime-honesty
gates (specs/0053/0056) REQUIRE the agent to run to prove the app is up. `execpolicy` classifies `curl` /
`node -c` as mutating (conservative — curl *can* POST), so they hit the ask tier and prompt. The guardian
(specs/0019) is already an AI reviewer that adjudicates ask-tier calls — but only HEADLESS; with a human
present it steps aside and prompts. The user's ask: "make the system agentic when it's asked this question"
— let the guardian answer the prompt, escalating only what it won't clear.

## Concepts

- **The guardian adjudicates in the REPL too.** When `CODE_GUARDIAN_INTERACTIVE` is on, `permissions._guardian`
  fires at depth 0 even when `ctx.interactive` — the same fail-closed reviewer subagent that judges the call
  against the user's request (APPROVE a reasonable, non-catastrophic step; DENY when it exceeds/deviates or
  is dangerous; when unsure, DENY).
- **Auto-approve the safe; DEFER the rest to the human.** In `_ask_approver`, an interactive guardian that
  APPROVES lets the call through (no prompt); one that does NOT approve returns `None`, so the call falls
  through to the human `[y/N]` — the human stays the backstop for anything the guardian won't clear. (Headless
  keeps the fail-closed deny: an unattended run must never proceed on an unreviewed action.)
- **The hard rails still win.** The guardian governs the ASK tier ONLY. Deny-rules, the workspace fence, the
  self-kill guard (specs/0050), and the mass-destruction cap (`CODE_GUARDIAN_MAX_DESTRUCTIVE`) all run
  regardless, so "agentic" never means "unbounded" — a destructive sweep, a `.env` write, or a fenced escape
  is still stopped.
- **Cost/latency.** Each ask-tier call now spawns a reviewer (a model call) instead of a prompt. Set
  `CODE_GUARDIAN_EFFORT=low` to keep it snappy; read-only commands still auto-allow with no guardian at all,
  so only the mutating/ask ones pay the review.

## Acceptance

New assertions in `scripts/check_guardian.py` (33/33), which now isolates `CODE_GUARDIAN_INTERACTIVE`:

- `CODE_GUARDIAN_INTERACTIVE` off (default): an interactive ask-tier call → `_guardian` returns `None` (not
  consulted, no spawn) — byte-identical to specs/0019.
- On: `_guardian` IS consulted when interactive (spawns, returns a verdict).
- On + APPROVE: `_ask_approver` auto-approves (no human prompt).
- On + DENY: `_ask_approver` returns `None` → defers to the human `[y/N]` (NOT a hard deny).
- All specs/0019 assertions (headless approve/deny, fail-closed, per-turn cache, recursion gate, ask-only,
  the mass-destruction cap, flag-off byte-identity) still hold.

## Non-goals

- Not a change to the PermissionRequest HOOK path (`_hooks_permreq` stays headless-only) — this is about the
  AI guardian, the "agentic" reviewer the user meant.
- Not a change to what the guardian APPROVES/DENIES (the review prompt is unchanged) — only WHERE it runs.
- No new hard-rail relaxation, no `SCHEMA_VERSION` bump, not in `safety_fingerprint`.

## Byte-identity

`CODE_GUARDIAN_INTERACTIVE` off (default): `_guardian` keeps its `not interactive` gate, so an interactive
run prompts the human exactly as before and a headless run is unchanged. Verified: the off assertion in
`check_guardian.py`, and the whole specs/0019 set unchanged (33/33). Requires `CODE_GUARDIAN` on to do
anything.
