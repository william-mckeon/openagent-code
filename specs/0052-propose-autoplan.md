# 0052 — propose first-approval backstop

Status: implemented
Flag: `CODE_PROPOSE_AUTOPLAN` (default off)

## Goal

Make propose mode stop collapsing into a read-only dead-end when the model never proposes. On a live
Inkling-Small Centpilot run the user asked to restart docker in `--mode propose`; the model went straight to
`edit_file` / `run_command`, every mutating call was hard-denied with "propose mode is read-only until the
manifest is approved," and NO approval prompt ever appeared — so from the user's seat propose was identical
to plan. The root cause (confirmed against the code): the ONLY thing that graduates a propose session from
read-only "investigate" to writable is the model calling `propose_changes` (the sole setter of
`propose_phase="approved"` / `propose_graduated=True`). If a weak model never calls it, there is nothing for
the user to approve and no affordance to intervene — the three specs/0048 relaxations all gate on
`propose_graduated`, which never flips. The user's ask: "propose should be like bypass, but I say yes/no on a
plan first."

## Concepts

- **The deny becomes an approvable one-item plan.** When `CODE_PROPOSE_AUTOPLAN` is on and a human is
  present, a mutating op denied in the investigate phase is turned into an interactive prompt —
  `[propose] approve this action and unlock the session? <tool>: <target> [y/N]` (`permissions._propose_autoplan`).
  A **yes** sets `ctx.propose_graduated` and ALLOWS this op; a **no** denies it and the session stays
  read-only. This is the missing first-graduation hinge — the user can now say yes to a plan without the
  model ever calling `propose_changes`.
- **Graduation relaxes every further op.** Once graduated (via autoplan OR `/approve` OR a real
  `propose_changes` approval), the three gate sites (`_propose_gate`, `_decide_command`, `decide_move`) treat
  `PROPOSE_AUTOPLAN and graduated` like the specs/0048 relaxations: further edits / commands / moves fall to
  the ask ladder (per-op `[y/N]`) instead of a hard-deny. So the first yes unlocks the session; subsequent
  ops each ask — exactly "like bypass, but I say yes/no."
- **A REPL `/approve` command.** `cli._repl_approve` lets the user graduate the session directly, without the
  model proposing at all — the guaranteed manual unlock when even the autoplan prompt hasn't been reached. It
  is advertised in the REPL banner only when `CODE_PROPOSE` and `CODE_PROPOSE_AUTOPLAN` are both on.
- **A model nudge on the denial.** When a propose read-only denial IS fed back to the model (headless, or the
  user declined the unlock), `agent.py` annotates the `Permission denied` result with an instruction to call
  `propose_changes` with a plan of the files AND commands (a build/run/restart counts) rather than retrying
  the raw op — so the weak model self-corrects toward the tool instead of looping on denials.
- **Under the hard rules, always.** The autoplan allow is reached only AFTER steps 1–2 (deny-rules + the
  workspace fence) have passed, at both the file gate (step 3b) and inside `_decide_command` (after its deny
  scan), so an unlocked op can never touch `.env`, escape the fence, or override a deny rule. Graduating never
  sets `propose_phase="approved"`, so the off-plan escalation net is untouched.

## Acceptance

Each item is an assertion in `scripts/check_propose_autoplan.py` (9/9, dep-free, no model), with the
specs/0022/0048 contract still proven by `scripts/check_propose.py` (63/63, now isolating `PROPOSE_AUTOPLAN`).

- Flag OFF: an investigate-phase edit/command is denied read-only and the ask channel is NEVER called — even
  interactive (byte-identical to specs/0022/0048); a graduated session with all relaxations off is still
  read-only-denied.
- Flag ON + interactive + **yes**: the op is allowed, `propose_graduated` becomes True, exactly one prompt is
  shown — for both the file gate and the command (docker-restart) gate.
- Flag ON + interactive + **no**: the op is denied ("declined") and the session stays locked.
- Flag ON + headless: no prompt, plain read-only deny (no human to approve).
- Flag ON + graduated: a further command is relaxed PAST the read-only gate (falls to the ask ladder).
- `CODE_PROPOSE_AUTOPLAN` defaults False when unset.

## Non-goals

- Not an auto-APPROVE — the user (or a hook) still says yes; autoplan only converts a dead-end deny into an
  askable one-item plan. Headless never auto-unlocks.
- Not a replacement for `propose_changes` — a real multi-file manifest is still the richer path and is
  unchanged; autoplan is the fallback for the run-shaped / never-proposed case.
- No `propose_phase="approved"` on an autoplan unlock, so `approved_paths` and the off-plan net keep their
  specs/0022 meaning; no `SCHEMA_VERSION` bump.

## Byte-identity

`CODE_PROPOSE_AUTOPLAN` off (default): `_propose_autoplan` returns None on its first line, the added
relaxation clauses are `False`, the agent nudge is skipped, and `/approve` reports it can't unlock — so every
propose ladder is byte-for-byte specs/0022 + specs/0048. Verified: `check_propose` 63/63 (with the new
isolation), `check_propose_autoplan` off-path assertions, `check_permissions` / `check_execpolicy` unchanged.
The flag is not added to `safety_fingerprint`.
