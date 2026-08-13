# 0093 — the model actually responds (narration fragment + grounding absence false-positive)

Status: implemented
Flags: `CODE_NARRATION_FULL_ANSWER` (+ `CODE_NARRATION_FULL_ANSWER_RETRIES`), `CODE_GROUND_ABSENCE_STRICT`. Both
default OFF → byte-identical.

## Problem

On the full-size Inkling (log `98d6cbd9d8a2`), the agent intermittently produces **no real answer** — a bare
header, a status line, or a phantom-flag rebuttal instead of the thing asked for:

- S2 T1 "list all files to add/update/delete" → the entire response is `=== FILE LIST: ADD / UPDATE / DELETE ===`
- S1 T9 "deep research + file list" → `Files in workspace: detailed_report.txt (5564 bytes)…`
- S1 T6 "it's a project proposal" → `File exists: detailed_report.txt (270 lines, readable)`
- T4/T5/T6 → a repeating **phantom** grounding challenge: `'detailed_report.txt' is described as missing/empty,
  but the file EXISTS on disk` — which the model never claimed.

(The mid-sentence cutoffs elsewhere in the log are just the logger clipping `result.final[:500]`, cli.py — not
truncation.)

### Two root causes

**A. `narration-as-final` (specs/0085) finalizes on the FIRST print.** In native tool mode a turn with a tool call
has `final=None`, so a weak model that "replies" by printing (`run_command(Write-Output "…")`) is honored: the
turn ends with that print's text (a clean `final`). But when the print is only a **header/opener** (`=== FILE
LIST ===`) or a **status line** (`File exists: X`), the model was about to print the body next — 0085 ends the
turn on the fragment and marks it `final`, so it isn't even flagged "incomplete." (It works when the model prints
the whole answer in one `Write-Output` — S2 T3.)

**B. `absence_contradictions` (grounding.py, `VERIFY_GROUNDING_PATHS`) false-positives.** It splits the answer into
sentences and flags any sentence with a cited existing path + an absence WORD (`_ABSENCE`). `_ABSENCE` is
deliberately OVER-triggering — harmless for the *semantic* verifier (it reads the file, returns GROUNDED) but NOT
here, where the deterministic path emits the challenge directly with no second opinion. The real trigger sentence
was: *"…fix the extraction artifacts in `detailed_report.txt`, **add missing code**?"* — "missing code" (code to
be *added*) next to the cited path → phantom "detailed_report.txt is missing/empty." It's **self-perpetuating**:
the model's rebuttal quotes the phantom claim, which re-matches next turn (fires T4, T5, T6). Root cause A then
ends the derailed turn on a status line.

## Fix (both gated, off → byte-identical)

**A. `CODE_NARRATION_FULL_ANSWER`** (agent.py). When narration-as-final would fire, if the printed text is a
FRAGMENT (`_narration_is_incomplete`: a `=== banner ===`, a lone markdown heading, a `File exists:` / `Files in
workspace:` / `Status:` status preamble, or a line ending in a "list follows" cue `: - — =`), NUDGE the model
(bounded by `NARRATION_FULL_ANSWER_RETRIES`, default 1) to deliver the COMPLETE answer as a normal reply instead
of finalizing on the fragment, then continue the loop. A **substantial** print (≥3 non-blank lines, or
substantive prose) is honored immediately — no needless nudge. The narration-**stall** guard (0067) remains the
backstop against a genuine loop. `_narration_is_incomplete` is conservative: when unsure, it treats the print as
complete (honor), so a real answer is never nudged.

**B. `CODE_GROUND_ABSENCE_STRICT`** (grounding.py). In `absence_contradictions`, a sentence is flagged only if it
carries a real absence PREDICATE on the path (`_ABSENCE_PREDICATE`: `X` is/are empty|missing|absent, does not
exist, has no source, no code/files) AND no rebuttal/action-negation markers (`_ABSENCE_META`: claim / described /
incorrect / "is not missing" / "did not open|read|review" / "only read"). This drops both the real-trigger class
("add missing code") and action-negations, breaks the self-perpetuating rebuttal loop, and keeps the genuine
"`src/auth` is empty / `main.go` missing / no Go source" catch.

## Acceptance

`scripts/check_actually_responds_0093.py` (20/20, dep-free, a scripted planner drives the real agent loop):
- `_narration_is_incomplete` returns True for the exact log fragments and False for a real multi-line/prose answer.
- **End-to-end**: flag ON, a header-print-then-full-print turn is NOT finalized on the header and delivers the
  complete answer; flag OFF is byte-identical (finalizes on the first print, as 0085 did); a substantive single
  print is honored immediately even ON.
- Grounding: the real-trigger sentence and an action-negation are flagged at baseline and DROPPED by strict; three
  genuine absences ("`X` is empty", "no code in `X`", "`X` has no implementation") are KEPT by strict.

## Non-goals

- Does not change WHY the weak model reply-prints (the advisory register 0092 + the nudge address that at the
  prompt/loop level); 0093 makes the loop robust when it happens.
- Does not touch the semantic verifier or `_ABSENCE` itself (still over-triggers for the semantic path, which is
  safe). Only the deterministic `absence_contradictions` consumer is tightened.
- The temp-file scratch discipline and the PowerShell shell-hint gaps seen in the same log are separate (spec 0094).
