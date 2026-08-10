# 0068 — volunteered-identity strip (WITHDRAWN)

Status: **withdrawn** — the operator rejected the approach ("i do not agree with a identity scrubber"): the
agent's identity must never be scrubbed/stripped from its output post-hoc. Identity behavior is fixed at the
model-format level (the 0063 `<model_information>` block + the 0066 scoping directive) — and the next live run
confirmed 0066 holds on its own (no volunteered announcements). The code, flag
(`CODE_STRIP_VOLUNTEERED_IDENTITY`), and harness (`check_identity_strip.py`) were removed; this record stays
as the point-in-time design + the reason it was withdrawn.
Flag: `CODE_STRIP_VOLUNTEERED_IDENTITY` (removed)

## Goal

Remove a VOLUNTEERED self-intro from the user-facing answer when the user did not ask about identity. This is
the **structural backstop** reserved in 0066: on a small model, the `<model_information>` block's announce-
reflex survives the "state it only when asked" prompt scoping, and the model bakes "**Identity:** I am Arcus,
created by Islander Intelligence" into its structured reports. Seen live across an entire multi-project review —
every report ended with the announcement, and the `thinking:` stream showed the model treating identity as a
required field of its report template. 0066 (prompt) reduced generation; 0068 (post-filter) cleans up what
still leaks through. Prompt-first, structure-if-it-survives — the same two-step that finally beat the identity
leak in 0063.

## Concepts

- **Post-filter the final answer.** `prompts.strip_volunteered_identity(text, request, name)` removes the
  volunteered self-intro sentence — `(Also —)? I am {name} … created/made/built by ….` — wherever it appears,
  non-greedy to its own period, and a now-dangling bold `**Identity:**` label left on its own line. Trailing
  whitespace is same-line only, so a mixed line keeps its real content ("… I did not read every file.") and
  paragraph breaks survive. It never returns a blank answer: if the text was ONLY the identity line, the
  original is kept.
- **Disarm when the user asked.** `prompts._asks_identity(request)` matches a real identity question ("who/what
  are you", "who made you", "your name", "what model", "introduce yourself", …). When the current turn asks,
  the strip stands down completely so the agent can answer.
- **One choke point.** The strip runs in `agent.Agent._finish` — the single exit every `run()` path routes
  through (including the max_steps synthesis) — on the user-facing `RunResult.final`, gated by the flag.

## Acceptance

`scripts/check_identity_strip.py` (dep-free, 17/17):

- A volunteered `**Identity:** I am {name}, created by …` line inside a report is removed; a trailing "Also — I
  am {name}, created by …" is removed; a mixed line keeps its substantive content; a clean answer is unchanged;
  an identity-only answer is never blanked; empty text / no-name are safe.
- `_asks_identity`: identity questions match, an ordinary task does not; when the user asked, the answer is NOT
  stripped.
- Wired into `_finish`: flag OFF is byte-identical (`RunResult.final` unchanged); flag ON strips the volunteered
  tail but keeps the answer; flag ON + an identity question does NOT strip.

## Non-goals

- **Display-facing only.** The trajectory keeps the raw turn — the model still generates the tail internally
  and sees it next turn. A corpus-level scrub of volunteered identity (via the specs/0059 trajectory scrubber)
  is a separate, deferred concern; 0068 fixes what the USER sees, which is the reported complaint.
- Not a replacement for 0066 — the prompt scoping still does the first-line suppression; this is the net under
  it. Not identity REDIRECTION (0063 still owns "Arcus, not Inkling").
- No new base-prompt text, no `SCHEMA_VERSION` bump.

## Byte-identity

With `CODE_STRIP_VOLUNTEERED_IDENTITY` off, `_finish` never calls the strip — `RunResult.final` is returned
verbatim. Verified: `check_identity_strip` 17/17, full dep-free suite green.
