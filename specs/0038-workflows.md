# 0038 — Workflows P1: a synchronous multi-phase engine

Status: accepted
Flag: `CODE_WORKFLOWS` (default **off**). Phase 1 of the workflows roadmap (P1 sync → P2 parallel →
P3 async/background → P4 front-end decoupling).

## Goal

Give the agent a `run_workflow` tool that runs a **deterministic, multi-phase** orchestration to digest a
large problem — the generalization of `review_repo` (a single fan-out) to **N ordered phases**, where each
phase fans out captured subagents and reduces them to a digest that **feeds the next phase**. The model
authors the plan (the phases, their jobs, the per-item instruction); the harness guarantees safe execution
(bounded fan-out, captured children, the lead never reads raw material itself — exactly the discipline that
keeps `review_repo` from overflowing the context).

P1 is **synchronous and in-turn**: `run_workflow` runs entirely inside its tool dispatch, blocking until the
final digest is ready, just like `review_repo`. Parallel fan-out (P2), background execution + notifications
(P3), and front-end decoupling (P4) are explicitly **out of scope here** — P1 needs no thread, queue,
registry, or REPL change.

## The spec the model authors

```
run_workflow(
  workflow = [
    { label, jobs: [ "<item>", ... ], instruction: "<per-item prompt>", focus?: "<lens>" },
    ...ordered phases...
  ],
  synthesis? = "<how the lead should synthesize the final digest>"
)
```

Each phase fans out one captured subagent per `job` (an item/question), each running `instruction` scoped to
that item, plus the **prior phase's digest as carry** so later phases build on earlier ones. The result is a
single digest the lead synthesizes next turn.

## Concepts

- **Pure planner seam** (`src/workflow.py`, model-free, deterministic, stub-testable — the shape of
  `orchestrator._areas`/`_balance_plan`/`_child_task`):
  - `plan_phases(spec)` → ordered phase list, capped at `MAX_WORKFLOW_PHASES`, empty/degenerate phases dropped.
  - `plan_jobs(phase, carry, cap)` → `[(label, child_prompt)]`, capped at `MAX_SUBAGENT_FANOUT`, degenerate
    scopes dropped (reusing `orchestrator._degenerate_scope`), returning the truncated remainder too.
  - `_job_task(item, instruction, focus, carry)` → the child prompt, which **always** appends the
    harness-owned length/format bound (under ~200 words, grounded) regardless of the model's `instruction`.
  - `assemble_digest(label, results, truncated)` / `final_digest(records, synthesis)` → the reduce.
- **Impure driver** `run_workflow(args, ctx)`: guards `ctx.spawn is None` and `ctx.depth >= 1` (a scoped
  child may not start a workflow — the same nesting guard as `review_repo`), a per-turn re-run guard on
  `ctx._workflow_digest`, then loops phases serially calling `ctx.spawn(child_prompt)`, threading each
  phase's digest forward as carry, and returns `ToolResult(True, final_digest)`. **Non-mutating** (stays out
  of `permissions.MUTATING`, like `review_repo`/`run_skill`); each child does its own gated work.
- **Capture** needs no `trajectory.py` change: the `run_workflow` tool_call (its spec + the returned digest)
  is logged by the agent loop like any tool, and each fanned-out child is already a linked trajectory via
  `ctx.spawn` (`parent_session_id` + `depth`). `SCHEMA_VERSION` is untouched — nothing about the record
  shapes changes.

## Acceptance (each a check in `scripts/check_workflows.py`)

1. Flag **off** → `run_workflow` is not in `active_tools()`; the base `TOOLS`/`tool_schemas` are unchanged
   and `Trajectory.SCHEMA_VERSION` is still `0.13.0` (byte-identical).
2. `plan_phases` preserves order and caps at `MAX_WORKFLOW_PHASES`.
3. `plan_jobs` caps at `MAX_SUBAGENT_FANOUT` (returning the truncated remainder) and drops degenerate scopes.
4. Every child prompt embeds its item **and** its instruction, and always carries the harness length bound
   **even when the instruction omits any length constraint**.
5. A 2-phase spec, driven by a recording stub spawn, fans out phase-1's jobs, reduces, and phase-1's carry
   text appears in phase-2's child prompts — with **zero** model calls.
6. `ctx.spawn is None` and `ctx.depth >= 1` both refuse; a second `run_workflow` in the same turn returns the
   cached digest.
7. `final_digest` contains every phase section + the synthesis trailer.

## Non-goals (later phases)

- **P2**: parallel fan-out (a shared bounded `fanout()` helper; `CODE_WORKFLOW_CONCURRENCY`).
- **P3**: background execution + notifications (a `TaskRegistry`, `/tasks`, drain-at-prompt); recommended as
  a **subprocess** per background workflow, not a daemon thread.
- **P4**: front-end decoupling — the runtime becomes a headless core with the REPL as one interchangeable
  front-end, so the UX is customizable. P3's registry/notification is deliberately designed as a
  front-end-agnostic seam to make P4 additive rather than a rewrite.
- Recorded debt: `run_workflow`, `review_repo`, and `run_skill` are now three parallel copies of the
  fan-out+reduce skeleton; a shared-reduce extraction is deferred to avoid churning `review_repo`'s
  byte-identical path.

## Byte-identity

`CODE_WORKFLOWS` off → the tool is never offered (single `toolset` gate), the system prompt adds nothing
(gated advertisement), `run_workflow` never runs, no `workflow` record is written, and `SCHEMA_VERSION`
is unchanged — every flag-off run is byte-identical. `docker/code/Dockerfile` is a deliberate non-edit
(no default-off flag is pinned there), as is `permissions.py` (the tool is non-mutating).
