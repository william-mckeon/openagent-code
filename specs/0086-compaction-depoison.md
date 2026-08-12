# 0086 — compaction / resume de-poison (clean the narration loop out of history)

Status: implemented
Flag: `CODE_COMPACT_DROP_NOISE` (default OFF → byte-identical)

## Goal

0085 stops a narration loop from FORMING (a reply-by-print ends the turn). 0086 cleans a session ALREADY
poisoned by one. In `logs/9f1891b0af8d.log` a resumed session's every turn began `[compact] summarized 778 msgs
~208000->8000 tok` — and those 778 messages are mostly the old narration loop: hundreds of no-op
`run_command(Write-Output "...")` turns plus the injected "STOP narrating" nudges. Summarized, that spam becomes
a loop-SATURATED briefing that re-primes the exact behavior, so a bare "hi" returned only *"No narration -
direct reply delivered."* — the model parroting its own poisoned context. No answer-extraction can fix a
poisoned INPUT; the fix is to stop feeding the model the loop.

## Concept

`src/context.py` — `drop_narration_noise(messages)` (pure, model-free): drop each no-op NARRATION turn (an
assistant whose ONLY tool_calls are pure `Write-Output`/`echo`/`write-host` prints, together with the tool
results that immediately follow it) and each STOP/narration NUDGE user message. Tool-call<->result PAIRING is
preserved (a narration assistant and its results are dropped as a unit, so nothing is orphaned — the invariant
`sanitize_tail`/Bedrock require). A real turn — any read/edit/run, any non-narration command, any user message —
is always kept. A strict no-op on a clean history.

Applied in two places, both gated on `CODE_COMPACT_DROP_NOISE`:
- **Resume** (`ContextManager.__init__`, `initial_working`): de-poison the rehydrated history so a looped session
  doesn't re-load (and re-summarize) its own loop.
- **Compaction** (`_compact`, before `model.summarize`): filter `old` so the summarizer sees the REAL work, not
  the loop; if `old` was ENTIRELY narration, the summary is a short "omitted" marker instead of a re-encoded
  loop. `old` leaves the working set via the summary either way, so pairing is unaffected.

## Acceptance

`scripts/check_depoison_0086.py` (10/10, dep-free): `drop_narration_noise` strips single- and multi-statement
narration turns + nudges, keeps the real work turn + result and the real user turn, and the output is
pairing-valid; a clean history is a strict no-op; a RESUMED `ContextManager` de-poisons its working set when on
and keeps the full history when off (byte-identical); and `_compact` feeds the summarizer the filtered history.
No regression: `check_context` 21/21, `check_resume` 13/13, `check_resume_0074` 10/10.

## Non-goals / honest scope

- **It removes STRUCTURAL noise (narration tool-calls + nudges), not fuzzy content.** A handful of recent
  assistant FINAL answers whose text is itself a meta-line ("No narration…", from 0085 converting a print) are
  prose, not tool-calls, so they survive. After the bulk (hundreds of turns) is gone the summary is
  work-dominated, but a DEEPLY poisoned session can still read thin — a **fresh session** is the reliable clean
  slate (the workspace files are on disk; only the conversation is discarded).
- Detecting arbitrary "meta-narration" content by heuristic is deliberately avoided (false-positives on real
  replies); 0086 only drops what is unambiguously a no-op.

## Byte-identity

`CODE_COMPACT_DROP_NOISE=false` (default): resume rehydrates the full history and `_compact` summarizes `old`
unchanged — identical to pre-0086. Verified: full dep-free suite green with the flag off.
