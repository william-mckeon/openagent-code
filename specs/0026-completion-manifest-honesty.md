# 0026 - completion & manifest honesty (a "done" claim must be backed by a real change)

## Goal
Close three seams a real multi-turn session exposed where the agent reported success it hadn't earned - so
neither the user nor (worse) the training corpus is told a change happened when it didn't:

1. **A false mutation claim with an empty ledger.** In propose mode, investigate phase, with zero writes,
   the closing answer *"Frontend folder copied to the working directory as requested."* was accepted and the
   run returned `terminated="final"`. No gate consults the mutation ledger for a prose completion claim.
2. **A partially-applied manifest reported as fully applied.** An approved 17-file manifest where one
   `apply_patch` failed still logged `approved=True` with all items and the missing add was invisible - the
   completion gate iterates `update_plan` steps, never the approved manifest, and `convert.py` drops only a
   DECLINED manifest, so a partial apply **trains as a completed change**.
3. **A dropped tool-call conflated with a deliberate finish.** On the final exhausted retry the model returns
   an empty response; the planner turns it into `final=""` and the agent routes it through `_finish` as
   `terminated="final"` (and logs the manifest approved) - an infra glitch recorded as a clean completion.

## Concepts
- **The unbacked-mutation net** (`grounding.unbacked_mutation_claim`, gated `CODE_VERIFY_MUTATION_CLAIMS`) -
  DETERMINISTIC, model-free, per-sentence, hedge-guarded. Flags a completed-mutation claim (created / copied /
  wrote / moved / deleted a file/folder/dir) **only when `ctx.mutations` is EMPTY** - the strongest, lowest-
  false-positive signal: the answer says it did a file op but zero file ops ran this turn. Returns `[]` the
  moment ANY real mutation happened (a partial apply is the manifest gate's job, below), so it never second-
  guesses a run that did change files. Rides the same `grounding.problems()` path as the absence / unverified-
  success nets.
- **The manifest reconciliation** (`agent._unapplied_manifest`, gated `CODE_VERIFY_MANIFEST`) - the manifest
  mirror of the completion gate (`_unverified_items`, which only sees `update_plan` steps). For an APPROVED
  manifest, each item whose target path (or a move's `from`) shows NO entry in `ctx.mutations` is unapplied;
  the completion challenge re-prompts (bounded by `CODE_VERIFY_COMPLETION_RETRIES`), then an honest outcome.
  In `_finish` the same check computes an `applied` boolean logged on the `manifest` record.
- **The dropped-response label** (`CODE_VERIFY_MANIFEST`) - `NativePlanner` marks `Decision.dropped` when a
  native-mode turn came back with empty content AND no tool calls (model.py's own `dropped` condition, at the
  planner). The agent returns an honest `no_output` for a dropped empty finish instead of `final`, so it
  isn't washed to `completed` and doesn't stamp a manifest approved off a glitch.
- **Corpus** - `train/convert.py` drops an APPROVED-but-UNAPPLIED manifest turn (`applied is False`) the same
  way it already drops a DECLINED one; `manifest_unapplied` + `no_output` join `GATE_OUTCOMES` so neither is
  relabelled `completed`. The `applied` field is OPTIONAL (absent when the flag was off at capture), so old
  trajectories and flag-off runs are byte-identical.

## Acceptance
- `src/grounding.py`: `unbacked_mutation_claim(final_text, mutations)` - `[]` when the ledger is non-empty or
  the text is hedged/absent; flags a completed file-mutation claim on an empty ledger. Wired into
  `problems()` behind `config.VERIFY_MUTATION_CLAIMS` (byte-identical off).
- `src/planner.py`: `Decision.dropped` (default False); `NativePlanner` sets it from the empty-content /
  no-tool-calls / schemas-present condition. `JsonPlanner` path unaffected.
- `src/agent.py`: `_unapplied_manifest(ctx)`; a `no_output` return for a dropped empty finish; the manifest
  items merged into the completion gate; `_finish` computes `applied` and passes it to `log_manifest`. All
  three gated on `config.VERIFY_MANIFEST`.
- `src/trajectory.py`: `log_manifest(..., applied=None)` writes the field ONLY when not None (byte-identical
  off); `SCHEMA_VERSION` -> 0.12.0 with a changelog line.
- `train/convert.py`: `_unapplied_manifest_turns` + the one-shot guard drop `applied is False` too;
  `manifest_unapplied` reason.
- `src/outcomes.py`: `manifest_unapplied` + `no_output` in `GATE_OUTCOMES`.
- `src/config.py` + `.env.example`: `CODE_VERIFY_MANIFEST` + `CODE_VERIFY_MUTATION_CLAIMS`, both default false.
- `scripts/check_completion_honesty.py` - dep-free, no model / no network.
- **Flag OFF is byte-identical**: with both flags false, no new net runs, no `applied` field is written, the
  dropped finish still returns `final`, and `convert` sees no `applied` key to gate on.

## Traps (each is a test)
- **Empty ledger is the trigger, not a keyword.** `unbacked_mutation_claim` returns `[]` the instant any
  mutation is recorded - it must not fire on a run that changed files (that is the manifest gate's job, which
  is per-item). Present-tense descriptions of what code does ("the Dockerfile creates the image") use no
  past-tense agent-completion verb and must not flag.
- **Hedge-guarded.** Reuse `_HEDGED`: "I will create", "you can copy", "to create", "should add" are not
  claims of a completed action.
- **Lenient manifest matching.** An item counts as applied if its `path` OR (for a move) its `from` appears in
  `ctx.mutations` - err toward "applied" so a correct apply is never spuriously challenged.
- **Optional `applied` field.** `log_manifest` writes `applied` only when computed (flag on + approved), so a
  flag-off / legacy `manifest` record is byte-identical and `convert`'s `applied is False` never matches it.
- **`no_output` is not `no_action`.** A dropped finish can follow real tool calls (`tool_calls > 0`), so it
  would classify as `completed`; it must be an explicit `GATE_OUTCOMES` label to stay out of the corpus.
- **Two flags, both default false.** `CODE_VERIFY_MANIFEST` (reconciliation + dropped label) and
  `CODE_VERIFY_MUTATION_CLAIMS` (the grounding net) are independent; either off is byte-identical for its part.

## Non-goals (v1)
- NL-parsing the answer to attribute WHICH file a completion claim names (specs/0007's rejected brittle path);
  the net is a coarse empty-ledger backstop, the manifest gate is the per-item check.
- Re-driving a failed `apply_patch` automatically (the reconciliation surfaces it; the agent re-applies).
- A dedicated retry of the dropped model call beyond model.py's existing backoff (this only labels the turn
  honestly once retries are exhausted).
