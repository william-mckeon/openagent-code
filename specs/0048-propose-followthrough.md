# 0048 — propose-followthrough

Status: implemented
Flags: `CODE_PROPOSE_RUN_AFTER_APPROVAL` (c), `CODE_PROPOSE_EXTEND_AFTER_APPROVAL` (b),
`CODE_PROPOSE_PERSIST_APPROVAL` (a) — all default off

## Goal

Fix the propose-mode dead-end: after the user approves a change manifest and it applies, the NEXT turn
re-locks to read-only, so a follow-up `run_command` (run/test the app just built) or `write_file` (extend
it) is denied with "propose mode is read-only until the manifest is approved" until a whole new manifest is
proposed and approved. The live Inkling Centpilot run hit this — the operator approved the file scaffold,
then "run it" and "build it" were both denied, forcing a switch to bypass mode. The root cause is
`agent.py`'s per-turn reset (`propose_phase = "investigate"` every turn); `specs/0022`'s Trap deliberately
made an approval never leak past its turn, which is correct for *file writes* but wrong for *running the
files you just approved* — a command is never a file-manifest item, so gating run/test behind file approval
was a category error.

## Concepts

- **A session-scoped `graduated` marker.** `ctx.propose_graduated` (default False, `tools.py`) flips True on
  the first manifest approval and — unlike `propose_phase` / `approved_paths` — is NOT cleared by the
  per-turn reset. Every relaxation below is consulted only when it is set, so a cold `--mode propose` still
  starts read-only until the first approval.
- **The per-turn reset is now one function.** `agent._reset_propose_for_turn(ctx)` holds the exact logic the
  reset block ran inline, so the harness can drive the cross-turn behavior directly. Default: clear
  `approved_paths`, set `propose_phase = "investigate"` — an approval never leaks. Under (a) on a graduated
  session: keep the approved phase + paths instead.
- **Three independent, opt-in relaxations** (all default off → byte-identical to specs/0022):
  - **(c) `RUN_AFTER_APPROVAL`** — a graduated, mutating `run_command` falls through to the normal ask
    ladder instead of hard-deny, on BOTH routing paths (`_decide_command` when EXECPOLICY is on, and
    `_propose_gate`'s command branch when it is off). The ask prompt IS the approval.
  - **(b) `EXTEND_AFTER_APPROVAL`** — a graduated OFF-manifest file mutation / move falls to the ask ladder
    (a per-edit prompt) instead of hard-deny. `approved_paths` is still reset each turn, so nothing is
    auto-allowed — every extension re-prompts.
  - **(a) `PERSIST_APPROVAL`** — the approved phase + `approved_paths` PERSIST across turns, so the
    signed-off files stay writable with NO further prompt all session (scoped bypass). Most permissive;
    supersedes (b)/(c) while on, because staying in the approved phase already relaxes everything.
- **Every relaxation sits UNDER the hard rules.** The deny-rules and the workspace fence run before the
  propose gate on all three ladders (permissions.py), so a relaxed op still cannot write `.env`, touch
  `.git/**`, or escape `cwd` + the granted roots. In headless runs a relaxed op hits the guardian and fails
  closed; the relaxation only helps an interactive session where the human answers the prompt (or, under
  (a), the pre-approval stands in for it).

## Acceptance

Each item is an assertion in `scripts/check_propose.py` (63/63, dep-free — the new section imports
`agent._reset_propose_for_turn` to drive the exact per-turn reset the old harness never exercised).

- Reset default (all off): an approved turn re-locks to investigate + clears `approved_paths` next turn;
  `propose_graduated` persists.
- Reset (a) on + graduated: the approved phase + `approved_paths` survive to the next turn; a NON-graduated
  (cold) session still re-locks (the cold guard holds).
- (c): a graduated mutating command is not hard-denied on either EXECPOLICY path; a cold session still is.
- (b): a graduated off-manifest write is not hard-denied; a cold session still is; a deny rule (`.env`) and
  the fence (outside path) still win under a graduated EXTEND.
- Flag-off byte-identity: a graduated session with all three off is still read-only-denied.

## Non-goals

- (b) does not auto-allow extension — it is always a per-edit prompt; relaxing below a prompt is (a).
- (a) is deliberately a "scoped bypass" and weaker than the specs/0022 guarantee; it is opt-in and off by
  default, and does not widen the deny/fence.
- The new flags are NOT added to `safety_fingerprint` (byte-identity with the existing corpus, matching the
  project's flag-provenance pattern) — a run's posture is inferred from the enabled flags, not the record.

## Byte-identity

All three flags default off, so `_reset_propose_for_turn` reduces to the pre-0048 inline reset and every
propose gate reproduces the specs/0022 deny. `propose_graduated` is set but read by nothing when the flags
are off. Verified: `check_propose` 63/63 (its flag-off byte-identity assertion + the untouched specs/0022
assertions), full suite unchanged, no `SCHEMA_VERSION` bump.

## Notes

Amends `specs/0022-propose-mode.md`'s Trap ("an approval never leaks to the next turn"): that remains the
default and the guarantee for file writes, but run/test (c) and — opt-in — extension (b) and scoped-bypass
(a) now carve out a graduated follow-through.
