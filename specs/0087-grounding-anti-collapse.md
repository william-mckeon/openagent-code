# 0087 — grounding anti-collapse / anti-hijack

Status: implemented
Flag: `CODE_GROUND_ANTI_COLLAPSE` (default OFF → byte-identical). Immediate relief also via
`CODE_VERIFY_GROUNDING_SEMANTIC=false`.

## Goal

Stop the grounding gate from eating the user's answer. Across FOUR sessions (logs `991aebe7b18c`, `95a98f5065c8`,
`9f1891b0af8d`, `df589704637f`, three projects, a week apart), a "review my project / what do you think" turn
delivered a **verification receipt** instead of the review:

- `991aebe7b18c:59` → *"Review complete. Confirmed: root style.css exists; 7 subfolder index files reference
  ../style.css…"*
- `95a98f5065c8:45` → *"Targeted confirmation: 0 .py files; Agent.py/tools.py absent…"*

Two defects combine:

1. **The semantic verifier FABRICATES filesystem facts.** It flagged `../style.css` as *"cited but not found"*
   ([991aebe7b18c:51](../logs/991aebe7b18c.log)) — but the model's own grep on the next line shows it exists at
   root. It flagged the correct claim *"no .py files"* as false, asserting *"Agent.py/tools.py present"*
   ([95a98f5065c8:41](../logs/95a98f5065c8.log)) — but they're absent; it confused a text MENTION of `Agent.py`
   in `projects/open-code.md` with the file existing.
2. **The correction re-prompt COLLAPSES the answer.** `grounding.challenge` said *"output your corrected answer
   to the CURRENT task and nothing else … keeping the rest as-is"*. The weak model read that as "emit just a
   'confirmed X' note" and threw away the whole review.

Net: the model builds a real review (e.g. via `review_repo`, `991aebe7b18c:50`), a flaky verifier false-flags one
incidental claim, and the user gets a receipt.

## Concept (all gated on `CODE_GROUND_ANTI_COLLAPSE`)

- **Deterministic cross-check** (`grounding.drop_contradicted_flags`, model-free): before a semantic flag can
  fire, check its FILESYSTEM claim against the real tree. Drop a flag that says a named file is
  "not found"/"missing" when that file EXISTS, or "present"/"exists" when NO such file exists. Matching is by
  PATH-SUFFIX (a specific `src/auth/config.py` isn't matched by an unrelated root `config.py`), and a
  CONTENT-absence claim ("validation missing IN auth.py", where the path is a location not the subject) is KEPT
  — file existence doesn't refute it, so the honest-but-wrong content class the gate exists to catch still
  fires. Fail-SAFE: only a provable FILESYSTEM falsehood is dropped. (Both refinements — suffix matching and the
  content-absence keep — came from an adversarial review of the first cut.)
- **Non-collapsing challenge** (`grounding.challenge` reworded): tell the model to RE-SEND its COMPLETE answer
  with only the flagged claim fixed, keep every other part written out word for word, and KEEP a claim that
  turns out correct — instead of "output the corrected answer and nothing else".
- **Collapse fallback** (`agent.py` + `_answer_collapsed`): remember the first substantial answer a challenge
  hit; if the correction still collapses it into a receipt (original ≥ 400 chars, reply < 40%), deliver the
  fuller ORIGINAL — but only after RE-VERIFYING it (`grounding.problems(original)` now clean), so a genuinely
  honest-but-wrong original is never resurrected as a clean `final` (the corpus-poison hole the review caught);
  otherwise keep the correction.

`.env` also ships `CODE_VERIFY_GROUNDING_SEMANTIC=false` for immediate relief — it disables the flaky verifier
outright while keeping the reliable deterministic path check (`CODE_VERIFY_GROUNDING_PATHS`); `0087` then still
protects that path and any future re-enable of the semantic verifier.

## Acceptance

`scripts/check_ground_anticollapse_0087.py` (12/12, dep-free, no model): `drop_contradicted_flags` drops the two
live false positives and KEEPS a genuine contradiction / a no-path flag / a correct absence flag; the challenge
is reworded when on and byte-identical off; `_answer_collapsed` detects a receipt; and end-to-end a collapsing
correction delivers the fuller ORIGINAL when on and the receipt when off. No regression: `check_grounding`
48/48, `check_grounding_paths` 17/17, `check_patch_grounding` 5/5.

## Non-goals / honest scope

- **The shallow stats-print review is NOT fixed here.** On the direct-read path (no `review_repo`), the weak
  model answers a review by PRINTING a stats block ("Files: 25, Functions: 0") which narration-as-final (0085)
  then delivers — because its real analysis stays in the reasoning channel, not the reply. That is a model
  limitation (Inkling-Small): a stronger model, or accepting terse reviews, is the honest answer. 0087 fixes the
  grounding HIJACK (which corrupted the `review_repo` synthesis path); it does not make a weak model write a
  richer review.
- The deterministic cross-check only reasons about PATH claims; a flaky verifier's non-path claim can still
  fire, which is why `VERIFY_GROUNDING_SEMANTIC=false` is the belt-and-suspenders immediate mitigation.

## Byte-identity

`CODE_GROUND_ANTI_COLLAPSE=false` (default): no flags dropped, the original challenge text, no collapse fallback
— identical to pre-0087. Verified: full dep-free suite green with the flag off.
