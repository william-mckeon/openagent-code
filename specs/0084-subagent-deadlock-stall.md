# 0084 — subagent propose-deadlock fix + no-progress stall breaker

Status: implemented
Flags (all default OFF → byte-identical): `CODE_SUBAGENT_NO_PROPOSE`, `CODE_STALL_MAX` (0=off),
`CODE_STALL_RETRIES` (default 1)

## Goal

Kill the "dramatic looping" root-caused from `logs/9f1891b0af8d.log` (Inkling-Small, "my portfolio"): a resumed
session that burned whole turns in a loop of denied mutations, blocked `propose_changes`, re-reads, and
fake-approval narration — measured **113** "propose mode is read-only" denials and **23** "propose_changes is
top-level only" failures across the log.

Two orthogonal fixes:

- **The deadlock (primary).** The completion/grounding gate AUTO-spawns a semantic grounding verifier via
  `grounding.semantic_problems → ctx.spawn` (no model tool call, which is why no spawn line appears in the log,
  only the "grounding verifier gave no usable verdict" fail-open lines). The serial spawn passed the parent's
  Permissions UNCHANGED (`subagent.py`), so a depth>0 child INHERITED **propose** mode — where it can neither
  mutate (read-only until the manifest is approved) NOR approve (`propose_changes` is top-level-only,
  `tools.py:1197`). The weak verifier ignored its read-only role, tried to do the task, and thrashed until
  max_steps. This exact trap was already documented at `permissions.py:372-381` (specs/0072 made the *message*
  depth-aware); 0084 removes the trap STRUCTURALLY.
- **No guard caught it.** The narration-stall guard (0067) counts only CONSECUTIVE wholly-pure Write-Output
  steps and resets on any interleaved read/denied step; the guardian denial-breaker (0080) is default-off and
  also resets on any allowed call (a failed `propose_changes` counts as allowed). So the loop — denied + failed
  + duplicate + narration, interleaved — was invisible to both.

## Concepts

- **`subagent._child_permissions(parent_permissions, read_only)`** (new, pure/testable). A parallel/read-only
  fan-out child (specs/0039) keeps its plan-mode `readonly_view()`. NEW: a serial child that would otherwise
  inherit **propose** mode ALSO gets that projection when `CODE_SUBAGENT_NO_PROPOSE` is on — plan mode denies
  mutations with the honest terminal "plan mode is read-only" (stop and report up), never the unsatisfiable
  propose bait. Any other serial child inherits the parent unchanged (off → byte-identical).
- **`grounding.py` / `guardian.py`**: when the flag is on, the auto-spawned verifier/reviewer spawns
  `read_only=True` in EVERY parent mode (a verifier must never mutate). The kwarg is passed only when the flag is
  on, so a flag-off call is byte-identical.
- **No-progress stall breaker** (`agent.py`, `CODE_STALL_MAX>0`). The general backstop the narration guard
  can't be. A step makes PROGRESS only if some call was allowed AND ok AND did NOVEL (not-seen-this-turn) real
  (non-narration) work; a denied / failed / pure-narration / DUPLICATE call is not. `ctx._stall_streak`
  increments on a no-progress step and — unlike the denial/narration counters — is NOT reset by an interleaved
  allowed-but-useless call. At `STALL_MAX` consecutive no-progress steps: one bounded nudge (`STALL_RETRIES`)
  then an honest `stall` outcome. Helpers: `_is_noop_narration` (also catches multi-line `Write-Output a;
  Write-Output b`, the 0067 escape) and `_canon_call` (the per-turn novelty key). `ctx._stall_streak` /
  `ctx._seen_calls` reset per task (no cross-turn leak).
- **`outcomes.py`**: `stall` joins `GATE_OUTCOMES` — an honest, non-trainable outcome `classify()` returns as-is
  (never washed to `completed`), so a stalled turn is dropped from the SFT corpus like `narration_stall`.
- **`cli.py`**: a REPL turn that ended on `max_steps`/`stall`/`narration_stall`/`denial_loop` now prints a short
  "stopped early — may be incomplete" note (parity with the one-shot path's `outcome=`), so a truncated recap
  isn't read as a finished answer (which drove the user to re-ask the same unbounded thing and re-loop).

## Acceptance

`scripts/check_stall_0084.py` (13/13, dep-free): the helpers; `stall` registration; `_child_permissions`
projects a propose parent to plan when on / inherits when off / always projects `read_only`; a projected child
denies a mutation WITHOUT the manifest bait; and end-to-end the breaker ends a DUPLICATE loop and a DENIED loop
as `stall`, leaves a genuinely-progressing (novel-read) run alone, and is byte-identical when `STALL_MAX=0`. No
regression: `check_narration_stall` 9/9, `check_guardian` 33/33, `check_grounding` 48/48, `check_permissions`
36/36.

## Non-goals

- **Not the read-scan efficiency device** (re-reading the same files across compaction/resume) — that is a
  separate follow-up (a session-scoped read-ledger, its own spec/flag); this spec STOPS the loop, it doesn't
  optimize a legitimate one.
- **Shell-idiom auto-correct** (`ls -la`, `/dev/null`, `||` on PowerShell) — a minor amplifier, deferred.
- **The stale-binary `edit_file KeyError`** seen in the log is already fixed in source (0072 required-arg guard);
  the operator fix is to REINSTALL the launcher, not a code change here.

## Byte-identity

All three flags default OFF: `_child_permissions` returns the parent unchanged for a serial child, the
grounding/guardian spawn calls pass no new kwarg, and the stall block is skipped (`STALL_MAX=0`). The cli note
fires only on already-abnormal `terminated` labels. Verified: full dep-free suite green with the flags off.
