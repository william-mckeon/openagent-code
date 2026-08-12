# 0088 — review substance (stop reviews collapsing into a receipt)

Status: implemented
Flag: `CODE_REVIEW_DELIVER_DIGEST` (default OFF → byte-identical) for the structural backstop; the prompt
rebalance is always-on (a prompt bugfix).

## Goal

Fix "you never told me what you think." A user asked for a whole-project review and got, every turn:
- turn 2 → *"Review complete. 9 folders covered. No edits made."*
- turn 4 → *"Review complete. 26 files across 9 folders + root verified. No edits made. Key findings: dead footer
  CSS, stub content..."*
(logs `6fa81b98849c`) — the model did the work (a `review_repo` fan-out whose children read every file) and even
drafted the review in its reasoning, but DELIVERED a status receipt.

Root cause (the user's own question — "why is the model so concerned about being concise"): the system prompt
ORDERS brevity. `prompts.py` said *"Be concise… Keep reviews and summaries tight — a short prioritized list
beats an exhaustive table"* and *"a concise architecture overview"*. A capable model reads that as "be tight";
Inkling-Small — which over-obeys literal instructions — reads it as "one line", collapsing the review to a
receipt, which narration-as-final (0085) then faithfully ships.

## Concept

- **Prompt rebalance** (`prompts.py` BASE_PROMPT + `orchestrator.py` review_repo trailer, always-on): stop
  ordering reviews to be terse. A review / audit / "what do you think" must give the ACTUAL assessment (a
  sentence or two per area, the overall take, the top findings with reasons); collapsing real work into a
  "Review complete, N files covered" receipt is called out as a FAILURE, not concision; and a review must never
  be answered by PRINTING a status line. Brevity is reserved for simple questions.
- **Structural backstop** (`CODE_REVIEW_DELIVER_DIGEST`): prompting can't be trusted on a weak model, so when a
  turn ran `review_repo` (its fan-out children actually read the files and returned a substantive per-area
  digest, cached on `ctx._reviewed_digest`) and the model's final answer is a receipt-sized COLLAPSE of that
  digest, deliver the per-area DIGEST itself instead — trailer stripped (`_review_digest_body`), prefixed
  "Here's the review, area by area:". Applied on BOTH delivery paths: the narration-as-final print (the live
  case) and a content receipt through the completion gate. A substantive synthesis (not a collapse) is kept.

## Acceptance

`scripts/check_review_digest_0088.py` (5/5, dep-free, no model): `_review_digest_body` keeps the per-area
summaries and drops the internal "You now have what you need" trailer; a receipt delivered as a PRINT
(narration-as-final) OR as CONTENT (completion) is replaced by the per-area digest when on; a substantive
synthesis is kept; flag OFF is byte-identical. No regression: `check_prompt_hygiene` 10/10, `check_workflows`
22/22, `check_narration_final_0085` 7/7, `check_grounding` 48/48.

## Non-goals / honest scope

- **The digest is per-area FINDINGS, not a polished opinion synthesis.** The backstop guarantees the user gets a
  real, file-grounded review instead of a receipt — but a genuinely insightful "what I think" synthesis that
  weighs the areas against each other is still a stronger-model capability. This makes the failure mode
  "substantive but unsynthesized" instead of "a one-line receipt".
- The prompt rebalance is a nudge; on a weak model that ignores soft guidance, the structural backstop is the
  load-bearing part.

## Byte-identity

`CODE_REVIEW_DELIVER_DIGEST=false`: no digest substitution on either delivery path — identical to pre-0088. The
prompt rebalance is a wording change to the always-on BASE_PROMPT / review trailer (a bugfix, not a gated
feature). Verified: full dep-free suite green with the flag off.
