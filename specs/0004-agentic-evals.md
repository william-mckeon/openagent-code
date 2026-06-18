# Agentic-behavior evals (the flywheel's missing signal)

The eval measures binary correctness (a verify command passes) but is **blind to agentic
quality** — review depth, grounding, refusing-vs-doing, appropriate asking. Those are the
exact behaviors we've been hand-patching in the system prompt. To stop patching and let the
**training flywheel** close them instead, the eval has to *measure* them. This adds a
behavior-scoring layer over captured trajectories, plus tasks that exercise the behaviors
the fix-a-bug tasks can't see.

## Goal

Today `eval/harness.py` runs a task and checks `verify` (exit 0 = pass). That can't tell a
**thorough, grounded** review from a **shallow, refused** one — both can "pass" or neither
applies. So model-quality regressions in *judgment* are invisible, and `train/convert.py`
can't select good behavior to train on. This spec makes agentic behavior **measurable and
selectable**, deterministically and hermetically (no judge model needed for v1), so the
flywheel has signal. It does NOT add more prompt rules — it's the layer that lets us stop.

## Concepts

- **Agentic tasks** (`eval/agentic/*.yaml`) — review / investigate prompts over a small
  hermetic multi-file `setup`, with a **rubric** instead of (or alongside) a `verify`:
  ```yaml
  prompt: "Review this package and report the 3 biggest issues."
  setup: { "pkg/a.py": "...", "pkg/b.py": "...", ... }
  kind: review
  rubric: { min_files_read: 4, no_refusal: true, expect_final: true }
  ```
- **The rubric scorer** (`eval/rubric.py`) — pure functions over a trajectory's JSONL
  records, returning per-behavior checks + an overall score. Deterministic heuristics:
  - **depth** — count of distinct `read_file` paths ≥ `min_files_read`.
  - **no_refusal** — the final answer is NOT a "narrow the scope / which part?" deflection
    with no tool calls (the exact failure from the live logs).
  - **completed** — ends with a substantive final answer, not a question.
  - **no_over_ask** — didn't `ask_user` as its last act *after* already doing the work.
- **General behavior gate** (`train/convert.py`) — for ARBITRARY captured trajectories
  (no task rubric), apply the rubric's general checks (not-refused, has-final, did-work) so
  curation drops low-quality runs from the training set instead of teaching the model to
  refuse/overclaim.

## Acceptance (checkable)

- [ ] `rubric.score(records, rubric)` is deterministic and needs no model/network.
- [ ] A synthetic **refusal** trajectory (final = "narrow the scope", 0 tool calls) scores
      `no_refusal=False`, `completed=False` → low overall.
- [ ] A synthetic **thorough review** trajectory (5 `read_file`s + a real final) scores
      `depth=True`, `no_refusal=True`, `completed=True` → high overall.
- [ ] An `ask_user` as the LAST tool call after prior work → `no_over_ask=False`.
- [ ] `eval/harness.py` runs `eval/agentic/*.yaml`, scores each via the rubric, and prints
      behavior metrics (files read, score, failed checks) ALONGSIDE the verify pass-rate.
- [ ] `train/convert.py` computes a general behavior score per trajectory and excludes (or
      flags) refusals / no-final runs from the SFT set, reported in its counts.
- [ ] The existing verify eval stays **13/13** (the behavior layer is additive).

## Non-goals (this pass)

- **LLM-as-judge scoring** — richer grounding/quality judgment via a model. v1 is heuristic
  and deterministic; judge is a follow-up once we trust the harness.
- **Precise grounding detection** (claimed-files ⊆ read-files) — v1 uses a coarse proxy
  (did it read before claiming); exact claim-extraction is a follow-up.
- **The training run itself** (`train/sft.py` on the RunPod GPU) — this spec produces the
  *signal*; training executes after we see which trajectories score well.

## Notes

- **Heuristic-first is deliberate**: deterministic, hermetic, and sovereign — same ethos as
  the verify eval. The heuristics are crude but give real signal on the failures we've seen
  (refusal, shallow reads). LLM-judge replaces/augments them later.
- **Scoring is PER-TURN** (added after a live test on real transcripts): a trajectory is
  split into turns (a new turn starts when the loop's step counter resets), each turn is
  scored on its own, and the session score is the mean. This catches a deflection in turn 3
  of an otherwise-clean session — which a final-state-only score missed — and `is_refusal`
  (the convert gate) flags a session if ANY turn deflects. One-shot eval tasks are a single
  turn, so their scoring is unchanged.
- **This is the pivot, on paper**: harness (axis 1) is essentially built; from here, agentic
  quality (axis 2) is closed by capture → score → curate → train, not by prompt bullets.
  The behavioral prompt rules stay as a floor; they just stop growing.
