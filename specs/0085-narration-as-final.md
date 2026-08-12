# 0085 — narration-as-final (the ROOT fix for the narration loop)

Status: implemented
Flag: `CODE_NARRATION_AS_FINAL` (default OFF → byte-identical)

## Goal

Fix the narration loop at its CAUSE, not with another counter. In `logs/9f1891b0af8d.log` (resumed session, a
bare "hi"), Inkling-Small emits the IDENTICAL `run_command(Write-Output 'No narration - direct reply delivered.')`
every step and never stops — even while its own visible reasoning says *"provide my final reply with ZERO tool
calls… No tool calls. This is correct."* Its reasoning and its emitted action diverge: the model **replies by
printing**, because a shell print is the only "say something to the user" channel it reliably produces.

Root cause (`src/planner.py:71`, native tool mode): a turn with ANY tool call returns `final=None`. So a
`run_command(Write-Output "…")` is classified as an ACTION — executed as a no-op, then the loop continues. The
model never emits a clean no-tool-call finish, so the turn can only end via a bandaid (the 0067 narration guard /
0084 stall breaker) or `max_steps`. The guards *end* the loop; they don't stop it from forming, and they burn
steps and replace the model's intended reply with "(Ended: I was repeating no-op narration…)".

## Concept

`src/agent.py`, right after `planner.step()`: when a step's ONLY calls are pure-narration prints
(`write-output`/`echo`/`write-host` of a literal, via `_is_noop_narration`), the model is trying to TALK, not
act. End the turn NOW with the printed text as a clean `final` — the FIRST time — so the loop never forms.
`_narration_text` pulls the printed strings out of the command(s) (`_PRINT_STMT`), which ARE the model's intended
user-facing message; if nothing is quoted, it falls back to the assistant content, else `"(done)"`. The turn
ends via the normal `_finish(..., "final", …)` path with a clean assistant message (no dangling tool_calls). It
does NOT run the print, does NOT go through the work-verification gates (a conversational reply isn't a
work-claim), and does NOT count toward any stall counter — there is nothing to count, the turn is already done.

This SUPERSEDES the narration-stall guard for the reply case (the turn ends before a streak can start); the 0067
and 0084 guards remain as backstops for non-narration loop shapes (denied / failed / duplicate churn).

## Acceptance

`scripts/check_narration_final_0085.py` (7/7, dep-free, no model): a scripted planner emitting the EXACT live
loop ends at **step 0** with `terminated='final'` and `final` equal to the printed text; the same planner with
the flag OFF does not end early (loops to `max_steps`, byte-identical); a real `Get-Content` step is never
converted; and `_narration_text` extracts single- and multi-statement prints. Full dep-free suite green; no
regression in `check_narration_stall` / `check_stall_0084`.

## Non-goals / honest scope

- **The poisoned resumed context is separate.** Every turn of this resumed session first compacts ~768 messages
  of loop history into a summary that re-primes narration. 0085 makes each such turn END cleanly at step 0
  (loop broken), but the reply can still be thin because the model's real answer lives in its reasoning channel.
  A fresh session avoids the priming entirely; a compaction-level de-poisoning is a possible follow-up.
- **Tradeoff (why it's opt-in):** a model that prints a genuine mid-task status would end the turn early. That
  only matters for a model that abuses prints — a capable model emits a real tool call to continue, or finishes
  with content and no tool call. So it ships default-OFF and is armed for the weak model that needs it.

## Byte-identity

`CODE_NARRATION_AS_FINAL=false` (default): the conversion block is skipped, the print runs as a command exactly
as before. Verified: full dep-free suite green with the flag off.
