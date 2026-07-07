# 0011 — Corpus curation (offline, deterministic)

> An offline batch pass over the captured trajectory corpus that flags PHANTOM CITATIONS — a closing
> answer referencing a file the run never opened. Deterministic, no model. Phase 11; the offline
> counterpart to the Phase-10 runtime gate, reusing the same `src/grounding.py` core.

## Why

Phase 10's grounding gate catches honest-but-wrong LIVE, on every completion. But the existing corpus
was captured BEFORE the gate, so it still holds ungrounded rows `is_trainable` can't see. Curation is
the offline pass that guards the distillation set.

Scope is deliberately narrow, and the reason matters. The *semantic* honest-but-wrong class (a real
file, the wrong facts — the init.sql case) needs a model to re-judge the answer against its sources.
Offline, the sandbox workspace is deleted after each run, so a batch judge has no files to read — a
tool-less `model.complete()` told to "read the compose file" would just hallucinate. So the SEMANTIC
class stays with the runtime gate (where the workspace is live), and offline curation does only the
DETERMINISTIC half: phantom citations, reconstructable from the records with no model.

## What it checks

A closing answer's cited paths (`grounding.cited_paths(strict=True)`) must be backed by evidence the
records carry. **Conservative by design** — a false DROP poisons the tiny corpus worse than a missed
phantom — so a citation is flagged ONLY if it appears in NEITHER:

- the **engaged-files** set (`grounding.touched_paths` — files an ok `read_file`/`write_file`/
  `edit_file`/`delete_file` names in its ARGS, normalized to match `cited_paths`), NOR
- any **tool listing** (the `[:4000]`-capped `tree`/`glob`/`grep`/`read` result content).

So discovery-without-read never causes a false drop; only a path the model referenced but *never saw
at all* is flagged. Facts come from tool-call ARGS (untruncated) + ok flags. The `trajectories/eval/`
firewall (specs/0005) is honored before any file is read. Two more false-drop guards: (1) a citation
matches evidence by EXACT path OR BASENAME (`grounding.grounded_by`), so a file engaged at `src/foo.py`
but cited as `foo.py` is grounded; (2) **subagent (`depth>0`) trajectories are skipped** — a Phase-10
grounding *verifier* cites paths it asserts are ABSENT by design, so curating it would flag it for doing
its job (mirrors the runtime gate's depth-0-only rule).

## Modes (`CODE_CURATE` opt-in, `CODE_CURATE_MODE`)

- **`flag`** (default) — `train/convert.py` stamps `meta.curation = {grounded, ungrounded}` on every
  row of the session; the corpus is never silently shrunk. Review the flagged rows yourself.
- **`exclude`** — `convert.is_trainable` drops an ungrounded session (reason `ungrounded_answer`),
  counted in `report.json`'s dropped ledger — no silent drops.

## Measurement: the `grounded_claims` rubric check

`eval/rubric.py` gains a deterministic per-turn `grounded_claims` check (opt-in per task via the rubric
key) using the SAME core: a cited path absent from the turn's engaged-files set fails it.
`eval/agentic/grounding_fidelity.yaml` arms it, so grounding fidelity is MEASURED in the promotion gate,
not just enforced. (The rubric uses the strict engaged-set only — its fixtures are controlled; the
curator layers the listing-conservatism for the uncontrolled corpus.)

## Files

- ADD `train/curate.py` — the offline pass + `curation_verdict(records)`.
- UPDATE `train/convert.py` — `is_trainable` (exclude) + `to_rows` (flag tag) consult curate.
- UPDATE `eval/rubric.py` — the `grounded_claims` check + `ungrounded_claims` surfacing.
- UPDATE `src/grounding.py` — shared `_norm` + `touched_paths` (offline existence oracle).
- ADD `eval/agentic/grounding_fidelity.yaml`, `scripts/check_curate.py`, `specs/0011-corpus-curation.md`.
- UPDATE `src/config.py`, `.env.example` (`CODE_CURATE`, `CODE_CURATE_MODE`), `ROADMAP.md`.

## Acceptance (`python scripts/check_curate.py`)

- [ ] `touched_paths` reconstructs engaged files (not listings), normalized.
- [ ] `curation_verdict` flags a phantom citation; clears a read / listed / no-citation one.
- [ ] A `./`-prefixed citation matches a plain read path (normalization parity — the mismatch guard).
- [ ] `convert.is_trainable` drops (exclude) / keeps+tags (flag) an ungrounded session.
- [ ] `rubric.grounded_claims` fails a phantom citation, passes a grounded one.
- [ ] Dep-free (no model, no network).

## Non-goals

- **Offline semantic re-judging** — unsound without the live workspace; stays with the runtime gate.
- **A model call** — curation is deterministic; the "120b-low judge" belongs to the runtime verifier
  (per-call effort + calibration), not here.
- **Multi-turn granularity** — the curator checks the session's closing answer; per-turn phantom
  citations are covered by the `grounded_claims` rubric check in eval.
