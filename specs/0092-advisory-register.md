# 0092 — advisory / conversational register (stop collapsing a thought-partner turn into a receipt)

Status: implemented
Flag: `CODE_ADVISORY_REGISTER` (default OFF → byte-identical)

## Problem

A live Centpilot run (log `7170fa4eb4dd`) used Arcus as a **research / design thought-partner** on a near-empty
workspace (one PDF): "review this PDF and make sure you understand it", "do deep research on the claims",
"what are your thoughts on the free version?", "give me more detail". Every substantive turn came back as a
**status receipt**, not an answer:

- turn 3 (deep research): `Claims verified: Legal — CONFIRMED` ×5, `Status: Ready for next instruction`
- turn 4 (summarize what you found): `=== SUMMARY FOR USER ===` … `Status: All claims verified. Plan is sound.`
- turn 5 (your thoughts on the free tier): `User asked specifically about FREE VERSION thoughts. Answer given: …`
  (third-person meta-narration *about* itself)
- turn 6 ("give me **more** detail"): `=== FINAL STATUS ===` … `Awaiting: User instruction` — *terser*, the
  opposite of the ask.

The research was actually done well (5 real web searches, correct figures) — then discarded and replaced with a
"CONFIRMED" receipt. The persona was empty (`CODE_AGENT_PERSONA=`), so this is **the prompt**, not a style.

### Root cause (why the prompt forces it)

The prompt models a code-editing task executor end to end and has **no advisory register**:

1. `native_tools_note` (appended to every native-mode prompt) pins the final reply: *"reply with a short final
   summary and no tool calls."* A design chat gets summarized into `=== SUMMARY ===`.
2. `LEAN_BASE_PROMPT` / `BASE_PROMPT` are entirely "a coding agent that edits real files… VERIFY: run the tests…
   report what you did and verified." Substance is granted only to a "review" ("a review earns substance",
   specs/0088) — a plain advisory question isn't a review, so it falls in the "quick answer = brief" bucket.
3. The verification vocabulary (`VERIFY / verified / COMPLETION IS VERIFIED`) bleeds into the user-facing voice —
   the model parrots it as `X — CONFIRMED`, `Status: …`.
4. The model "replies by printing" (`run_command(Write-Output "…")`, honored by specs/0085) — and a printed
   message is a status dump by nature, reinforcing the receipt shape.

## Concept (all gated on `config.ADVISORY_REGISTER`, off → byte-identical)

- **`native_tools_note`** — a ternary: when armed the closing line says the final message is *"the ANSWER to what
  was asked: prose that explains or advises when the user asked you to think… a short summary of the change when
  it was a code task. Not a status line."* Off → the original "short final summary" wording verbatim.
- **`build_system_prompt`** — appends one **ADVISORY REGISTER** note when armed: explain / research / weigh /
  "what do you think" is not a code task → answer in substantive prose with the reasons; do NOT reply with a
  receipt (no ✓ checklists, no "X — CONFIRMED / verified", no "=== SUMMARY ===", no "Status / Awaiting"); keep the
  VERIFY/verified/done vocabulary to internal discipline; "answer directly" means lead with substance, not omit
  the reasoning; write the reply as prose in the final message, never `Write-Output` it. Off → the note is absent.

No change to the base-prompt string constants, the gates, or the reasoning-leak machinery — the fix is purely the
missing *register*, added additively when armed.

## Non-goals

- Does NOT loosen the honesty/verification gates, the read-only-review rule, or the grounding contract — those are
  correct; the fix keeps their *vocabulary* out of the user-facing voice, not the discipline itself.
- Does NOT touch the anti-reasoning-leak detectors (`has_reasoning_leak` etc.); the note only clarifies that
  "answer directly" permits reasoned prose, it doesn't disable leak detection.
- Not a separate "chat mode" — one register note, so a code task still behaves exactly as before.

## Acceptance

`scripts/check_advisory_register_0092.py` (dep-free): armed → the assembled native prompt contains the ADVISORY
REGISTER note and `native_tools_note` no longer pins "a short final summary" (says "the ANSWER to what was
asked"); the note explicitly forbids the receipt tells (CONFIRMED / === SUMMARY === / Status-Awaiting / ✓
checklist) and the Write-Output-as-reply habit; OFF → `build_system_prompt` and `native_tools_note` are
**byte-identical** to the pre-0092 text (the exact "short final summary … ends the session." string, no advisory
note). No regression across the prompt-adjacent suite.

## Byte-identity

`CODE_ADVISORY_REGISTER=false`: `native_tools_note` returns the original literal and `build_system_prompt` appends
no note. Verified: full dep-free suite green with the flag off.
