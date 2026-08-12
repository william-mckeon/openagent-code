# 0089 — lean system prompt (strip the firehose)

Status: implemented
Flag: `CODE_LEAN_PROMPT` (default OFF → byte-identical)

## Goal

BASE_PROMPT grew, across ~88 specs, to **9,802 chars / 116 lines** — sent to the model on EVERY turn before it
can do anything. An exhaustive extraction of every model-facing prompt in `src/` (13 files) found BASE_PROMPT is
"the single biggest bloat source": the honesty/verify theme is restated across ~7 bullets and review-behavior
across ~6, and "a model treats each restatement as a fresh literal constraint, amplifying over-obedience and the
be-substantial-vs-be-concise contradiction." That over-obedience is exactly what produced the recent failures
(reviews collapsing to a receipt, etc.). The prompt was drowning the model.

## Concept

`LEAN_BASE_PROMPT` — the load-bearing behavior only, ~81% smaller (1,833 vs 9,802 chars): a role line + six
tight bullets — read-before-you-claim (+ path form), workspace scoping, edit/delete mechanics + update_plan,
verify-don't-declare, review = read-only + substantive (+ review_repo), answer directly / match length. It keeps
the SAME opening identity line, so the name substitution and the `<model_information>` identity block (0063)
inject verbatim; the tool-mode suffix, memory, todos, spec, and persona wiring are unchanged. `build_system_prompt`
selects it when `CODE_LEAN_PROMPT` is on; OFF returns the full BASE_PROMPT, byte-identical.

What was dropped is not lost behavior — it is redundant elaboration the gates/tools already enforce (coverage
honesty and negative-verification → the grounding gate; dependency-dir avoidance → the search tools already hide
them; don't-declare-broken-from-a-fragment, the granted-dir paragraph, the "don't think out loud" restatements).

## Acceptance

`scripts/check_lean_prompt_0089.py` (14/14, dep-free): LEAN is ≥70% smaller; keeps the identity/name anchors and
every load-bearing token (read_file/edit_file/delete_file/`rm`/verify/review_repo/read-only/workspace) and the
0088 no-receipt intent; and `build_system_prompt` embeds the full BASE_PROMPT when off (byte-identical) and the
lean prompt when on. No regression across the 10 prompt-adjacent harnesses (`check_naming` isolates the flag like
it already isolates the identity block).

## Non-goals / next

- This strips the ~10K/turn CORE. The same extraction flagged more reducible surface — `json_tools_protocol`
  (~1.1k), the grounding verifier task + challenge, the review_repo trailer, the compaction/synthesis prompts,
  and several tool `description` fields — all TRIM candidates for a follow-on, behind the same idea. BASE_PROMPT
  was the 80% win and ships first.
- Lean is a wording change, so there is no automated proof it doesn't regress behavior on the real model — it is
  REVERSIBLE by design (`CODE_LEAN_PROMPT=false`) precisely so lean-vs-full can be compared on the live model.

## Byte-identity

`CODE_LEAN_PROMPT=false` (default): `build_system_prompt` uses BASE_PROMPT exactly as before. Verified: full
dep-free suite green with the flag off.
