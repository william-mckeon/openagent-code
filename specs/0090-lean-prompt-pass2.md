# 0090 — lean prompt, pass 2 (the secondary prompts)

Status: implemented
Flag: `CODE_LEAN_PROMPT` (same flag as 0089; default OFF → byte-identical)

## Goal

0089 stripped BASE_PROMPT (the 65% chunk). The full prompt inventory (48,214 chars across 13 files) flagged the
remaining always-used surface: **tool descriptions (8,558 — 27 descriptions the model reads every turn)**, the
`build_system_prompt` per-tool notes, the injected PowerShell rules, the review_repo trailer, and the grounding
challenge. This pass leans those under the SAME `CODE_LEAN_PROMPT` flag. Net: the assembled system prompt (native
mode, all 25 tools) drops **17,993 → 9,203 chars — 48%** — on top of the lean BASE_PROMPT.

## Concept (all gated on `config.LEAN_PROMPT`)

- **Tool descriptions** (`tools._LEAN_DESC` + `desc_for`): a map of 15 shorter descriptions; `desc_for(t)` returns
  the lean one when armed, the full one otherwise. Both consumers use it — `openai_schemas` (native) and
  `prompts.json_tools_protocol` (json). Tool NAMES and arg schemas are untouched, and every machine contract
  survives: the `apply_patch` envelope (`*** Begin Patch` … hunk markers), the `pursue` argv-LIST bar examples,
  the `web_fetch`/`web_search` untrusted-data + cite + weak/strong contract, `update_plan`'s per-step `file`
  (the completion-gate hook), `delete_file`'s never-`rm`. `edit_file` (no lean variant) stays verbatim.
- **build_system_prompt notes**: lean WEB / PROPOSE CHANGES / SPEC-FIRST variants that keep the load-bearing
  behavior (web = untrusted data, cite URLs; propose required-in-propose-mode; spec can't-report-done-until-
  acceptance-met), each a `lean if config.LEAN_PROMPT else full` ternary.
- **PowerShell rules** (`envcontext.build_env_context(lean=…)`, threaded from agent.py): keeps the non-inferable
  footguns (`;` not `&&`; a bare `echo` HANGS; `Stop-Process -Id` not by name; no `2>&1` on a native exe) and
  drops the alias catalog (cat/ls/grep/head/mkdir -p/…) a model already knows.
- **review_repo trailer** (`orchestrator.py`): a 2-sentence lean trailer that KEEPS the `"\nYou now have what you
  need."` prefix (the anchor `agent._review_digest_body` / specs 0088 strips on) and the trailing
  `+ prompts.reply_shape_caveat()`.
- **grounding challenge** (`grounding.py`, GROUND_ANTI_COLLAPSE branch): a shorter re-prompt that keeps the 0087
  "RE-SEND your COMPLETE answer, keep a correct claim" intent.

## Acceptance

`scripts/check_lean_prompt2_0090.py` (14/14, dep-free): tool descriptions shrink when armed and are byte-identical
off; edit_file (no lean) is unchanged; every listed contract survives in the lean text; the assembled system
prompt is ≥30% smaller; the PowerShell lean keeps the footguns and drops the alias catalog; the grounding
challenge stays shorter but keeps RE-SEND. No regression across the prompt-adjacent suite (read_tools, situational,
propose, workflows, grounding, web_coupling, specs, prompt_hygiene). An adversarial-review workflow checked each
lean rewrite against its consumers/contracts.

## Non-goals / next

- NOT leaned (deliberately KEEP, per the inventory): `json_tools_protocol` (the action protocol), the
  prompt-hygiene block, planner nudges, the compaction prompt, the identity block, the grounding OUTPUT contract
  and guardian verdict tokens (machine-parsed), the web untrusted-content fences, `edit_file`'s description.
- Deferred TRIM candidates: SUMMARIZE/SYNTHESIS prompts, the guardian criteria prose, the workflow/skills
  trailers, and the agent narration/stall nudges — lower-traffic, safe to lean in a later pass.
- The 2 dead-code CUTs (a pre-0087 challenge fallback, deprecated `unbacked_mutation_claim`) are left for a
  separate structural cleanup — they change flag-off behavior, so they don't belong in a lean-gated pass.

## Byte-identity

`CODE_LEAN_PROMPT=false`: `desc_for` returns the full description and every note/trailer/challenge/PowerShell
branch falls to the original text. Verified: full dep-free suite green with the flag off.
