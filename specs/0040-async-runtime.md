# 0040 — Workflows P3: an async background runtime (REPL-only)

Status: accepted
Flag: `CODE_WORKFLOWS_ASYNC` (default **off**, REPL-only) + `CODE_MAX_BACKGROUND_TASKS` (cap, default 3).
Phase 3 of the workflows roadmap (P1 sync → P2 parallel → **P3 async/background** → P4 front-end).

## Goal

Let a workflow run in the **background** and **notify** when it finishes — so you submit a long digest and
keep working — **without rewriting the blocking REPL loop and without a new tool.** Async submission is a
runtime *strategy inside `run_workflow`*, so its schema, the logged `tool_schemas`, and the safety
fingerprint are byte-identical in both flag states; `toolset.py`/`tools.py`/`context.py`/`agent.py`/
`trajectory.py` are untouched.

## Concepts

- **`src/tasks.py`** — a PURE `TaskRegistry` state machine (`queued → running → done | error`, illegal
  jumps on a terminal/unknown id refused) + PURE formatters (`banner_line`, `render_tasks`,
  `render_result`, `fold_result`), driven through an **injected** result-reader and a `Popen` factory so the
  harness exercises everything with a `FakePopen` and an in-memory dict — no real subprocess/thread/model.
- **Submit** — in `run_workflow`, after `plan_phases` validation and *before* the inline fan-out, a branch
  gated on `config.WORKFLOWS_ASYNC and ctx.interactive and ctx.task_registry is not None and ctx.depth == 0
  and os.environ.get("_OAC_BG_WORKER") != "1"` serializes the phases to a spec file, registers a task,
  `Popen`-launches a worker, and returns a **task-id immediately**. Otherwise it falls through to the
  unchanged synchronous loop.
- **Subprocess per task** (not a daemon thread) — `python -m src --run-task <id> <spec> -C <ws> --mode plan`
  with `_OAC_BG_WORKER=1`. Its own `Permissions`/MCP/litellm+boto3 globals and one-writer-per-`Trajectory` at
  the OS level. Recursion is bounded **three ways** (a worker is non-interactive, carries `_OAC_BG_WORKER=1`,
  has no registry) so its own `run_workflow` can never re-enter the async branch. Workers are **read-only**
  (`--mode plan`) so a background workflow can't race the foreground agent on workspace files.
- **Notify** — a loop-top banner drained *before* `input()` (drain-then-prompt, drain-once via an
  `_announced` flag, mirroring the change-gated project-todos re-surface), plus `/tasks` (list) and
  `/result <id>` (pull). **Latency:** the banner fires at the next prompt, not real-time into a blocked
  `input()`.
- **Delivery** (the only Bedrock-safe path) — `/result <id>` pulls the finished digest, prints it, and
  arms a fold; `fold_result` prepends it into the **next user task** as ONE `role:"user"` message
  (`CONTEXT from … / My request: …`). That single `cm.add({"role":"user"})` + `set_task` at the clean
  top-of-turn boundary is structurally never a consecutive-user 400, the 0035-fix-C bleed, or a mid-array
  `role:"system"` rejection. **Auto-inject is rejected** — splicing mid-turn would create the dangling-tool
  / consecutive-user sequence `rollback` + `sanitize_tail` exist to kill.

## Guardrails (from the adversarial pass)

- **Lifecycle:** on session end, if tasks are still running, **prompt** the user — *keep them running after
  exit* (they finish + write result files unattended) or *cancel* them (`terminate()` + `wait`). No human
  present → cancel (never leave orphans hitting Bedrock unattended). (Windows does not kill children on
  parent exit.)
- **Result-file grace:** an exited worker whose result file isn't visible yet (OneDrive lag) stays `running`
  for a small grace window (`_GRACE_POLLS`) before latching to `error`.
- **Byte-identity of the help line:** the `/tasks  /result <id>` suffix on the startup command list is gated
  on the flag; the registry is only built under the flag, so flag-off runs execute zero new lines.
- **Fold durability:** pending pulled results are cleared only after a *successful* `agent.run`.

## Acceptance (`scripts/check_async.py`, dep-free, no model/subprocess)

1. Legal transitions (`queued→running→done`/`error`) and illegal-jump refusal on a terminal/unknown id.
2. `submit` refuses past `MAX_BACKGROUND_TASKS`.
3. `refresh` via a `FakePopen` + in-memory result: exit-0-with-result → `done`; exit-nonzero → `error`;
   exit-0-no-result stays `running` through the grace window, then `error`.
4. `drain_finished` surfaces each finish exactly once.
5. `fold_result` yields ONE `user`-framed CONTEXT string (never a bare consecutive-user or system turn);
   `render_tasks`/`render_result`/`banner_line` carry the id + state.
6. Byte-identity: `run_workflow` in `active_tools()` in **both** flag states; `SCHEMA_VERSION` unchanged;
   `safety_fingerprint` unchanged; `CODE_WORKFLOWS_ASYNC` defaults False against the fallback.

## Non-goals

- No auto-inject; no real-time push into a blocked `input()`; no re-attaching a background task to a *future*
  REPL session (results land in `trajectories/tasks/` files). No `SCHEMA_VERSION`/`safety_fingerprint` change.
- P4 (front-end decoupling).

## Byte-identity

`CODE_WORKFLOWS_ASYNC` off → the registry is never built, the submit branch is dead (`run_workflow` runs its
unchanged inline loop), the drain/commands/help-suffix/teardown execute zero lines, and no result/tool
schema changes. `docker/code/Dockerfile` is a deliberate no-op — P3 is `ctx.interactive`-gated and the
container ENTRYPOINT is the non-interactive one-shot path, so a background submit can never fire in-container.
