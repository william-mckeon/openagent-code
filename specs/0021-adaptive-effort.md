# 0021 — adaptive reasoning effort (match effort to the task)

## Goal
Reasoning effort is fixed for a run today (`config.REASONING_EFFORT`). That's wrong in both directions: it
over-thinks a one-line lookup and under-thinks a broad refactor or a subtle bug. This phase makes effort
ADAPTIVE — raised to match a hard task, left cheap for routine work — through **one dial** (`Model.effort`,
which model.py re-reads on every call) driven by **two triggers**:

- **The agent self-escalates** — an `escalate_effort` tool it calls when it sizes up a hard task (model
  proposes; the same shape as `pursue`/`update_plan`). A sticky per-turn request.
- **The harness auto-escalates on struggle** — deterministic, from signals `agent.run` already tracks:
  consecutive tool failures, the completion/auto-verify/grounding re-prompt counters, and a goal bar that
  keeps failing. This is where the run is already burning steps, and it doesn't depend on the model's
  self-awareness.

Both feed **one pluggable Policy**, so "how effort is decided" is a switch, not a hardcode.

## The pluggable policy (the switchable part)
`CODE_EFFORT_POLICY` selects the decider: `off` | `reactive` (deterministic default) | `online` (the
opt-in learner) | a dotted `module:Class` an operator wrote. A bad/missing/erroring choice **falls back to
reactive** — a policy never crashes the run. The deterministic policies live in `src/effort.py`; the
**online learner is a separate module (`src/effort_online.py`) imported ONLY when selected**, so the
default path stays deterministic and pinnable, and *self-learning is a switch each operator flips.*

## Self-learning: the flywheel, not an in-harness RL loop
The escalation decisions are captured as a first-class signal (`effort_change` records + the `effort`
field on each `model_call`) so the **distilled model learns the effort policy** — a task shaped like THIS
needed more thinking. That's self-learning in this project's paradigm: it happens at distillation,
deterministically, from labeled trajectories. The reactive auto-escalator is the *teacher*; the tool-based
self-escalation is what the student learns. The **online learner** (`effort_online`) is an opt-in,
per-project shortcut for operators who want feedback faster than a retrain: it records whether an escalated
turn then SUCCEEDED (keyed by a coarse task signature) and PRE-escalates a signature that has historically
needed it. It is a simple reference learner (a per-signature win-rate), not an RL system — and it's behind
a switch precisely so the pinnable default is never compromised.

## Acceptance
- `src/effort.py`: `LADDER` (ordered — config `_EFFORTS` is an unordered set), `rank`/`cap`/`_bump`/
  `_higher`/`resolve_baseline`/`struggle_score`; `Policy` interface; `OffPolicy`, `ReactivePolicy`
  (escalate-only, threshold-gated, capped, stateless); `load_policy()` (switchable + fail-safe).
- `src/effort_online.py`: `OnlinePolicy` — `decide` (reactive + learned pre-escalation), `update`
  (per-signature win-rate), JSON persistence to `CODE_EFFORT_STATE`, never raises.
- `src/tools.py`: `escalate_effort` (registration-only: stashes `ctx.effort`, escalate-only) + `EFFORT_TOOLS`.
- `src/toolset.py`: offers `EFFORT_TOOLS` only when `CODE_ADAPTIVE_EFFORT` and the policy isn't `off`.
- `src/agent.py`: snapshot the as-built effort at `__init__`; **restore it in the per-task reset** (never
  leak a bump into the next turn); the APPLY point at the loop top — **depth-0 only** (a subagent's
  `GUARDIAN_/GROUNDING_EFFORT` is untouched), **escalate-only + monotonic** within a turn (a non-escalating
  turn is byte-identical), gated on the flag + a real planner model; `_finish` feeds the policy the outcome.
- `src/model.py`/`src/trajectory.py`: log the resolved `effort` per `model_call` + an `effort_change`
  record (schema 0.9.0). `train/convert.py`: thread `effort` into the step meta (neutral to keep/drop).
- `scripts/check_effort.py` + `scripts/check_effort_online.py` — dep-free, no model/network.
- **Flag OFF is byte-identical**: the tool isn't offered, the apply point never runs, effort is unchanged.

## Traps (each is a test)
- **Cross-turn leak** — Agent+Model are built ONCE, `run()` is per turn; a bumped effort must be restored
  to the **snapshot** (not `config.REASONING_EFFORT` — a subagent is built with its own effort).
- **Subagent clobber** — the guardian/grounding subagents run through the same `run()`; the apply point is
  **depth-0 only** so their explicit effort isn't auto-escalated.
- **`looks_degenerate` is terminal** — it `return`s, so it is NOT an escalate-and-continue trigger; only
  the gate re-prompts + `consecutive_fail` (+ the goal bar) are.
- **Log the effort field, not `None`** — thread the *resolved* effort (`self.effort or REASONING_EFFORT`).

## Non-goals (v1)
- An in-harness RL/bandit that retunes the DEFAULT policy live (non-deterministic; fights the pinned
  harness discipline). The online learner is opt-in and isolated.
- Effort BELOW the operator's floor (escalate-only) or above the cap.
- Nested self-escalation (depth-0 only).
