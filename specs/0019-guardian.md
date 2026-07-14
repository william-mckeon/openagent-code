# 0019 — guardian (fail-closed approval reviewer for the ask tier)

## Goal
Give the `ask` tier a captured, automated reviewer so an **unattended** run can proceed on a *reviewed*
ask-tier action instead of only blocking. When a tool call hits `ask` (a human would normally be
prompted), the guardian spawns a CAPTURED reviewer subagent that judges the specific request and returns
APPROVE / DENY. Unlike the grounding gate — which fails OPEN (an infra hiccup never traps the agent,
because completion already proved the work was done) — the guardian fails **CLOSED**: no reviewer, an
error, a timeout, or any verdict that isn't a clean APPROVE → DENY. It governs the `ask` tier ONLY (never
allow / deny / read-only), so it can only make a run MORE restrictive, never less. Reviews ride the same
captured-subagent path as the grounding verifier, so every approval decision is a first-class trajectory
that feeds the flywheel.

## Acceptance
- `src/guardian.py`: `review(tool, target, reason, ctx) -> bool` — spawns a captured reviewer via
  `ctx.spawn` (with `CODE_GUARDIAN_EFFORT`), parses its verdict, and returns True only on a clean
  APPROVE. Fail-CLOSED: no spawn / raise / empty / subagent-error / a `DENY` anywhere / an ambiguous
  `APPROVE … but DENY` / prose-with-no-verdict → **False**. Pure of side effects, never raises.
- `src/permissions.py`: every `ask`-tier / prompt site (the ask rule + the default-mode baseline in
  `decide()`, the two in `_decide_command`, the `decide_move` prompt) consults the guardian FIRST when
  `CODE_GUARDIAN` is on and `ctx.depth == 0`; otherwise it falls through to the interactive human prompt
  / headless block **unchanged**. It never overrides a deny rule or the fence (ask tier only).
- **Recursion-safe**: the reviewer runs at `depth+1`, and the guardian is gated on `depth == 0`, so the
  reviewer's own tool calls never re-enter the guardian.
- **Flag OFF (default) is byte-identical to today** — the guardian is a pre-branch that returns None when
  off, leaving the human-prompt / block path exactly as it was.
- `scripts/check_guardian.py` proves: APPROVE→allow, DENY→block, every failure mode → DENY (fail-closed),
  markdown-tolerant + ambiguous-denies verdict parsing, the depth>0 recursion gate, ask-tier-only (a deny
  rule still blocks under an APPROVE), and flag-off parity. Dep-free, no model, no network.

## Non-goals
- Reviewing allow / deny / read-only tiers — ask ONLY.
- Replacing the fence, the deny rules, or execpolicy/sandbox — the guardian sits ON TOP of them, at the
  approval point.
- A perfectly-calibrated reviewer — that is model quality (the flywheel). The harness guarantees only the
  fail-closed CONTRACT: uncertainty always denies.

## Notes
- Off by default (`CODE_GUARDIAN=false`), like every adoption-track phase.
- Pairs naturally with execpolicy (0016) + the expanded `ask` tier: the ops now on `ask` (rm/sudo/curl/
  wget/.git/.env-delete) are exactly what the guardian reviews for an unattended run.
