# 0091 — subagent budget (keep the main agent premium, make spawned children cheap)

Status: implemented
Flags: `CODE_SUBAGENT_EFFORT` (default `""` → inherit), `CODE_SUBAGENT_MAX_STEPS` (default `0` → inherit). Both
default OFF → byte-identical.

## Problem

Running Arcus on Inkling-Small (Tinker, pay-per-token) is expensive, and the largest single multiplier is
**subagent fan-out**. A `review_repo` covers up to `CODE_MAX_REVIEW_AREAS` (16) top-level areas, and **each area
is a full agent loop** — its own many model↔tool round-trips. `spawn_agent`/`run_workflow` fan out too. Every one
of those children is built by `runtime.build_agent`, which:

- constructs `Model(trajectory, effort=effort, …)` — and with `effort=None` the child inherits the **global
  reasoning pin** (`CODE_REASONING_VALUE=xhigh`, the max, 0.99), the biggest OUTPUT-token driver; and
- runs to `config.MAX_STEPS` (50) — so a stuck child can burn the whole step budget.

But a subagent's work is cheap by nature: read a folder, summarize it, or render an APPROVE/DENY verdict. It does
**not** need max reasoning or 50 steps. The main agent, which does need the quality, was being taxed the same as
every throwaway child. There was no per-subagent effort knob — only the two explicit per-role ones
(`GROUNDING_EFFORT`, `GUARDIAN_EFFORT`) — so a generic spawned child had no way to run cheaper.

## Goal

Keep the **main agent** at its premium pin (untouched), and let every **spawned subagent** default to a cheap
effort and a smaller step budget — tunable from `.env`, and a no-op (byte-identical) when unset.

## Concept

Two new config knobs and a three-line wiring:

- **`config.SUBAGENT_EFFORT`** — validated against `_EFFORTS` (the per-role ladder `{low, medium, high}`, same
  set/pattern as `GUARDIAN_EFFORT` / `GROUNDING_EFFORT`); `""` when unset or invalid. Note this is NOT the global
  `CODE_REASONING_VALUE` pass-through path (which additionally accepts `xhigh` / a float), so `xhigh`/`minimal` are
  **not** valid here. An out-of-set value **warns to stderr and falls to `""`** (mirroring `_env_int`) — without
  that warning a typo like `CODE_SUBAGENT_EFFORT=xhigh` would *silently* leave subagents on the expensive global
  pin, the exact opposite of this knob's purpose (an adversarial review caught this; the first cut validated
  silently and the `.env.example` doc wrongly promised `minimal|…|xhigh`).
- **`config.SUBAGENT_MAX_STEPS`** — `_env_int`, floored at 0; `0` when unset.
- **`subagent.run_subagent`**: at the top, `if effort is None and config.SUBAGENT_EFFORT: effort = config.SUBAGENT_EFFORT`.
  A caller that **pinned** an effort (the grounding verifier passes `GROUNDING_EFFORT`, the guardian passes
  `GUARDIAN_EFFORT`) still wins — those pass a non-None `effort`, so the default never overrides them. It also
  passes `max_steps=(config.SUBAGENT_MAX_STEPS or None)` into `build_agent`.
- **`runtime.build_agent`**: new optional `max_steps=None` param; the returned `Agent` uses
  `config.MAX_STEPS if max_steps is None else max_steps`. The main/resume callers never pass it → unchanged.

Explicit-effort precedence is the crux: `SUBAGENT_EFFORT` fills in only the `None` case, so it lowers the *generic*
child (a `spawn_agent` worker, a `review_repo`/workflow fan-out child) without disturbing the per-role verifiers
that deliberately choose their own effort.

## Cost knobs armed alongside (config-only, no code — they already exist)

The live `.env` also tightens the subagent **count** and the guardian effort, all pre-existing knobs:

- `CODE_SUBAGENT_EFFORT=low`, `CODE_SUBAGENT_MAX_STEPS=12` — the new knobs.
- `CODE_GUARDIAN_EFFORT=low` — the guardian verdict doesn't need premium reasoning.
- `CODE_MAX_REVIEW_AREAS=16 → 6` — cover the top 6 areas, not up to 16.
- `CODE_MAX_SUBAGENT_FANOUT=8 → 3`, `CODE_MAX_SUBAGENT_DEPTH=2 → 1` — fewer, flatter ad-hoc children.

The main agent's `CODE_REASONING_VALUE=xhigh` and `CODE_MAX_STEPS=50` are **untouched**.

## Acceptance

`scripts/check_subagent_budget_0091.py` (dep-free): with `SUBAGENT_EFFORT` set, a spawned child's `Model` is built
with that effort; an explicit caller effort (grounding/guardian) overrides it; unset → the child effort is `None`
(inherits the global pin) — byte-identical; with `SUBAGENT_MAX_STEPS` set, the child `Agent` gets the capped step
budget and the global `MAX_STEPS` is not passed; unset → `build_agent` byte-identical for the main/resume callers.
It also exercises the real env-parse gate (via `importlib.reload`): `CODE_SUBAGENT_EFFORT=xhigh` → `""` **and**
a stderr warning; `=low` → `"low"` with no warning; and a doc-truth check that `.env.example` names `low|medium|high`
and never re-promises `minimal`/`xhigh`. No regression across the subagent-adjacent suite (async, fanout, guardian,
grounding, spawn).

## Byte-identity

`CODE_SUBAGENT_EFFORT` empty and `CODE_SUBAGENT_MAX_STEPS=0` (the defaults): `run_subagent` leaves `effort` as the
caller passed it and passes `max_steps=None`, and `build_agent(max_steps=None)` uses `config.MAX_STEPS` — the exact
prior construction. Verified: full dep-free suite green with the flags at their defaults.

## Non-goals / next

- Does not change what a subagent is *allowed* to do (permissions/read-only projection are unchanged — see
  specs/0039, 0084), only how hard it thinks and how long it may run.
- The remaining cost levers (main-agent effort ladder / adaptive effort, compaction threshold, prompt caching,
  the read-ledger) are separate specs, tracked in the cost-efficiency plan.
