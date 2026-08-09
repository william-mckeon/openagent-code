# 0067 — narration-stall guard

Status: implemented
Flag: `CODE_GUARD_NARRATION_STALL` (default off) + `CODE_NARRATION_STALL_MAX` (3) / `CODE_NARRATION_STALL_RETRIES` (1)

## Goal

Stop a weak model that has finished its work but won't END the turn from filling dead air with side-effect-free
shell narration. Seen live (Inkling-Small, a portfolio-review run): after reading the files, the agent needed
only to ask "which part next?" — instead it emitted **dozens** of `run_command('Write-Output "Status: …"')` /
`'Write-Output "Review… (A/B/C)"'` calls in a row, burned its way into a mid-run compaction, and never produced
a clean final answer. This is the cousin of the greenfield re-listing loop (0058) and the text-repetition
degeneracy guard (already in `agent.py`): a model that can't TERMINATE substitutes busywork for a finish. Each
no-op command also enters the trajectory — corpus poison.

## Concepts

- **Detect a pure-narration command.** `agent._is_narration_command(cmd)` is True only for an obvious
  side-effect-free print — `Write-Output "…"` / `echo '…'` / `Write-Host "…"` — with NO pipe, redirect, chain
  (`;`), or subshell (`$(`). Conservative by construction: a command that reads (`Get-Content`, `ls`), writes,
  or feeds another cmdlet never trips it.
- **Count consecutive narration-only steps.** After each step's tool calls run, if the step's ONLY action was
  pure-narration `run_command`(s), `ctx._narration_streak` increments; any real action resets it to 0 (reset
  per task, like the other per-turn counters, so nothing leaks across turns).
- **Nudge, then end honestly.** At `CODE_NARRATION_STALL_MAX` (3) consecutive narration steps, a bounded nudge
  (`CODE_NARRATION_STALL_RETRIES`, default 1) tells the model to STOP narrating and either write its answer or
  ask its question directly (a question IS a normal final answer, not something to narrate). If it keeps
  narrating past the retry budget, the run ends as **`narration_stall`** — a new honest gate outcome, so it is
  never washed to `completed` and is dropped from SFT (`outcomes.GATE_OUTCOMES`).

## Acceptance

`scripts/check_narration_stall.py` (dep-free, 6/6): a scripted planner emits a pure `Write-Output` every step
against a fake registry.

- `_is_narration_command`: `Write-Output`/`echo`/`Write-Host` of a literal are narration; a read, a pipe, a
  redirect, a subshell, or a `;`-chain are NOT.
- `narration_stall` is in `GATE_OUTCOMES` and `classify` returns it as-is (not washed to completed).
- Flag ON: the pure-narration loop ends as `narration_stall` (not `max_steps`, not `completed`).
- Flag OFF: byte-identical — the guard never runs; the same loop just spends its steps → `max_steps`.
- A repeated REAL command (`Get-Content`) is never flagged, even with the guard on.

## Non-goals

- Not a general anti-verbosity or anti-repetition measure (the text-repetition degeneracy guard already exists);
  this targets specifically the no-op-command stall.
- Not a block on `Write-Output` — a single legitimate print, or narration mixed with real actions, never trips
  it. Only `MAX` consecutive narration-ONLY steps do.
- No `SCHEMA_VERSION` bump; the new outcome is additive to `GATE_OUTCOMES`.

## Byte-identity

With `CODE_GUARD_NARRATION_STALL` off, the guard block is skipped entirely and `_narration_streak` is never
consulted — byte-for-byte the prior loop. Verified: `check_narration_stall` 6/6, full dep-free suite green.
