# 0032 - declared-done verification (the completion/manifest/acceptance/goal gates are ONE pattern)

## Goal
Name the architectural spine that Phases 7 / 20 / 25 / 26 grew independently, so future verification work fits
a mold instead of accreting ad-hoc gates - and formally RETIRE the one member that never fit (the free-text
mutation-claim net). This is a CONCEPT + deprecation spec: it documents an invariant and adjusts docs, and it
deliberately does NOT refactor the working gate chain (see Non-goals).

## Concepts
- **The family.** Four checks in `agent.py`'s `if not decision.calls:` chain are the SAME shape - the agent
  DECLARES what "done" means in a STRUCTURED artifact, and the harness holds "done" until reality backs each
  item, looping (bounded re-prompt) instead of accepting the claim:

  | Gate (spec) | Declared artifact | Backed by (reality) | Function | Honest outcome |
  |---|---|---|---|---|
  | Completion (0007) | `update_plan` steps | the mutation ledger | `_unverified_items` | `unverified_completion` |
  | Manifest (0026) | approved `propose_changes` items | the mutation ledger | `_unapplied_manifest` | `manifest_unapplied` |
  | Acceptance (0025) | `write_spec` acceptance items | their marks | `_unmet_acceptance` | `acceptance_unmet` |
  | Goal loop (0020) | a `pursue` bar | a command's exit code | `goal.run_bar` | `goal_unmet` |

  The **completion gate (0007) is the base case**; 0025 and 0026 are it GENERALIZED to richer artifacts (a
  spec, a manifest). `_unapplied_manifest` even merges into `_unverified_items`' `unmet` list and shares its
  `verify_retries` counter - the same loop, literally.
- **The invariant.** Verification ALWAYS attaches to a structured artifact the agent declared (a plan / a
  manifest / a spec / a bar), and each gate keeps a DISTINCT honest outcome in `outcomes.GATE_OUTCOMES` so the
  corpus (`train/convert.py`) drops the right turns. A new verification phase MUST add a structured artifact +
  its own honest outcome - never a free-text heuristic.
- **The retired member.** The mutation-claim net (`grounding.unbacked_mutation_claim`,
  `CODE_VERIFY_MUTATION_CLAIMS`, Phase 26) is the ONE check that anchors on PROSE, not a declared artifact -
  the "brittle NL parsing" specs/0007 rejected. A live smoke test showed it false-flagging ordinary
  descriptive prose ("prints all saved notes") into `ungrounded_completion`. It is DEPRECATED: default OFF,
  documented as superseded by the family, kept only as an opt-in backstop. The one case it uniquely caught -
  a propose-INVESTIGATE prose claim ("I copied the folder") with no manifest - is properly a STRUCTURED
  concern: propose mode already requires `propose_changes` before a change, so a bare prose claim with no
  manifest is a protocol miss the propose discipline owns, not something an English regex should guess at.

## Acceptance
- `specs/0032-declared-done-verification.md`: this document - the family, the invariant, the deprecation.
- `src/config.py`: the `CODE_VERIFY_MUTATION_CLAIMS` comment marks it DEPRECATED / superseded by the
  structured family (specs/0032); default stays false.
- `src/grounding.py`: `unbacked_mutation_claim`'s docstring notes it is the deprecated NL outlier.
- `.env.example`: the mutation-net flag is documented as deprecated; the structured checks are the recommended
  path.
- `scripts/check_declared_done.py` - asserts the family INVARIANT (each structured gate's honest outcome is
  present in `GATE_OUTCOMES` and the four are DISTINCT; the mutation flag's CODE default is false). Dep-free.
- **Byte-identical**: no gate-chain behavior changes - only docs/comments, a new spec, and a new harness. The
  runtime toggle (a user flipping `CODE_VERIFY_MUTATION_CLAIMS` off) is config, not code.

## Traps (each is a test)
- **Distinct outcomes.** `unverified_completion` / `manifest_unapplied` / `acceptance_unmet` / `goal_unmet`
  must stay DISTINCT and all in `GATE_OUTCOMES` - collapsing them would blur the training signal.
- **The mutation flag stays default-off in CODE.** The deprecation is documentation; the code default is
  already false, and this spec must not change gate behavior.
- **Structured, not prose.** The invariant is that verification attaches to a declared artifact. This spec is
  the reference a future phase checks itself against before adding a check.

## Non-goals (deliberate)
- **Physically merging the gates into one function.** They already ARE the pattern, expressed as a sequential
  chain. A merge collides with the hardest invariants - each gate's DISTINCT honest outcome (the corpus
  depends on it), its own flag (`VERIFY_COMPLETION` / `VERIFY_MANIFEST` / `SPEC_FIRST` / `GOAL_LOOP`), its own
  retry counter, and byte-identical-off - for a payoff (less duplication) that doesn't justify rewriting the
  highest-risk file in the repo. Unify the CONCEPT, not the code.
- **Deleting the mutation-net code.** It stays as an opt-in, hardened backstop (default off). Deprecation is a
  doc + a default, not a removal - so an operator who wants the unstructured backstop can still enable it.
- **Building the structured propose-investigate protocol check** now (the clean replacement for the one case
  the NL net caught). Noted here as the RIGHT future shape; not built in this spec.
