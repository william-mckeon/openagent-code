# 0039 — Workflows P2: bounded parallel fan-out (read-only when concurrent)

Status: accepted
Flag: `CODE_WORKFLOW_CONCURRENCY` (default **1** = serial = byte-identical). Phase 2 of the workflows
roadmap (P1 sync → **P2 parallel** → P3 async/background → P4 front-end decoupling). Folds in the "P2.1"
read-only guard so `run_workflow`/`review_repo` can parallelize safely, not just `run_skill`.

## Goal

Make the per-phase / per-area fan-out children run **concurrently** instead of serially, bounded, via one
shared helper — so a digest/review workflow finishes in wall-clock ≈ its slowest child, not the sum. The
three existing serial fan-outs (`workflow.run_workflow` inner loop, `orchestrator.review_repo`,
`skills.run_skill`) all route through the same helper.

## The safety rule: parallel ⟹ read-only

Parallel children that could **write** would race the shared filesystem (two children editing under
auto-approve). Only `run_skill` children are read-only *by construction* (their prompt says "do NOT edit,
create, or run anything", skills.py:158); `review_repo`/`run_workflow` children hold the full write toolset.
So the guard is uniform and enforced in ONE place: **when `fanout` runs in parallel (`max_workers > 1`) it
spawns each child `read_only=True`**, which builds the child with a read-only `Permissions` projection
(`Permissions.readonly_view()` — plan-mode enforcement: every mutating tool denied, the fence + rules
preserved). Reads/reviews run in parallel; a workflow that must *write* runs serially (`=1`).

## Concepts

- **`src/fanout.py`** — `fanout(spawn, tasks, max_workers)` → results **positionally aligned to `tasks` in
  submission order**. Stdlib-only (`concurrent.futures`), no `config`/model import, so the harness drives it
  with a fake spawn.
  - `max_workers ≤ 1` (or ≤ 1 task): literally `[spawn(t) for t in tasks]` — **no executor constructed** →
    byte-identical spawn order + results + digest to the serial loops it replaces.
  - `max_workers > 1`: a bounded `ThreadPoolExecutor`; submit in order, gather in submission order (so the
    reduce is deterministic regardless of completion order); each child spawned `read_only=True`. A raising
    task re-surfaces at its own slot while already-submitted siblings still run to completion.
- **`Permissions.readonly_view()`** (`src/permissions.py`) — a fresh `Permissions` at `mode="plan"` (which
  denies every mutating tool at the ladder's plan step) carrying this object's `deny`/`ask`/`allow` +
  `extra_roots` + `read_only_roots`. The original is untouched.
- **`run_subagent(..., read_only=False)`** (`src/subagent.py`) — when `read_only`, the child's `Permissions`
  (and its safety fingerprint) is the projection; `ctx.spawn(task, read_only=…)` threads the flag through.
- **MCP under concurrency** (`src/mcp_client.py`) — dispatch is already thread-safe
  (`run_coroutine_threadsafe`), but one `ClientSession` per server multiplexes a single stdio transport, so
  `_call_sync` acquires a module lock to stop concurrent children interleaving frames. Uncontended (and
  thus invariant) at concurrency 1.

Thread-safety otherwise holds by construction: `run_subagent` gives each child its OWN `Context`,
`Trajectory` (own `.jsonl` handle), and agent/model — no shared mutable writer. The `litellm` module client
cache is warmed by the lead's first (single-threaded) call, then hit warm by the children on the same key.

## Acceptance (`scripts/check_fanout.py`, dep-free, no model)

1. **Serial byte-identity** (`max_workers=1`): a lock-guarded enter/exit log shows peak concurrency = 1 and
   perfectly-nested order; `results == [spawn(t) for t in tasks]`.
2. **Real overlap** (`max_workers=2`): two workers rendezvous on a `threading.Barrier(2)` (a secretly-serial
   pool would time out → `BrokenBarrierError`); results still come back in **submission** order.
3. **Exception isolation** (`max_workers>1`): one task raises; every non-raising sibling still completed
   (pool not torn down) and the exception surfaces at that task's slot.
4. **Read-only when parallel**: the fake spawn records its `read_only` arg — `True` for every parallel
   spawn, `False`/default for every serial spawn.
5. **`readonly_view` enforcement**: the projection denies `write_file`/`delete_file` and allows
   `read_file`/`grep`.

## Non-goals (later)

- The process-wide **Bedrock concurrency semaphore** (`CODE_MODEL_MAX_CONCURRENCY`) — deferred; the only
  unbounded burst is bounded by `CODE_WORKFLOW_CONCURRENCY` (≤ `MAX_REVIEW_AREAS`) and throttle 503s are
  already retried. Add if throttling appears.
- P3 (async/background + notify), P4 (front-end decoupling).

## Byte-identity

`CODE_WORKFLOW_CONCURRENCY=1` (default) → `fanout` is `[spawn(t) for t in tasks]`, the executor is never
constructed, `read_only` is never set, the MCP lock is uncontended → spawn order, results, and every digest
are unchanged. `readonly_view` is only reached from the parallel path. No `safety_fingerprint` change, no
`SCHEMA_VERSION` bump. `docker/code/Dockerfile` is a deliberate no-op (default-1 is serial/safe).
