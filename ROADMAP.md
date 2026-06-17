# openagent-code — ROADMAP

The canonical plan. This file is the source of truth for *what we're building and
in what order, and why*. Update it as phases close so the plan survives across
sessions and never has to be reconstructed from memory.

## Goal

A self-hosted coding agent that can do what Claude Code does **as an agent** —
the harness capabilities, independent of raw model quality — running on a model
**you** own, with every run captured as training data. We measure parity by
*agent capabilities*, not by matching the underlying model's reasoning.

## Two axes (keep them separate)

- **Agent capabilities** — the harness: tools, loop, orchestration, memory, etc.
  This is the finite, countable gap to "what Claude Code does." It's the target.
- **Model quality** — closed by the training flywheel, asymptotically, and mostly
  on *your* task distribution. NOT what we measure parity by, but it's why the
  flywheel exists: every agent-capability run also produces training data.

## The flywheel

```
run agent → capture trajectory → convert to rows → (train) → eval → repeat
```

Capture and outcome-labeling are done. Eval is done. The converter is the current
piece. Training itself is downstream.

---

## Phases (the committed sequence)

### Phase 0 — working agent ✅ DONE
Native tool-calling on self-hosted gpt-oss-120b, six tools, exact-match edits,
honest outcome labels, trajectory capture, Docker. Runs the investigate→fix→verify
loop end to end.

### Phase 1 — measurement (eval) ✅ DONE
`eval/harness.py` + a now-**13-task** spread: 8 easy single-edit tasks (regression
tier) + 5 harder discriminating tasks (multi-file rename, coordinated two-edit,
boundary edge-cases, multi-fix planning, regression-guard) whose verifies reject a
*plausible-but-wrong* fix. Baseline **13/13 (100%)** in native mode, trajectories
persisted to `trajectories/eval/`.
Caveat (still open): the harder tier sharpened the verifies and raised the *effort*
(12-15 tool calls vs 6-9), but gpt-oss-120b clears it too — so the number is still
pinned at 100% and can't yet discriminate. A **genuinely-hard tier** (find where the
model actually breaks) is the next eval calibration so the pass-rate can finally move.

### Phase 2 — SFT converter ✅ DONE ("close the SFT")
`train/convert.py`: filter trainable trajectories → flatten to conversational SFT
rows → write dataset + auditable report. Built **forward-compatible**: it already
prefers tool schemas logged in `session_start` and falls back to reattaching the
current `src/tools.py` schemas — so the Phase-3 gate lands with zero rework here.
"Closed" = it produces a clean, validated dataset from the eval batch with a
counts report and no silent drops.

### Phase 3 — the self-containment GATE ✅ DONE  (before any agent-capability work)
Log the full tool schemas once in `session_start` (`src/trajectory.py`); the
canonical eval batch is now self-contained (`schema_version` 0.2.0).

Done: `SCHEMA_VERSION` → 0.2.0, `session_start` carries `tool_schemas`, converter
confirmed `tool_schema_source=logged`, eval still 5/5. **Both halves of the gate
closed:** (1) future runs are self-contained; (2) the pre-gate `0.1.0` eval
trajectories were **deleted** so nothing toolset-fragile survives into Phase 4.

**Why both halves matter:** reattachment (the converter's fallback for schema-less
trajectories) is correct only while the toolset is unchanged. Phase 4 changes the
toolset (tool breadth), at which point any leftover `0.1.0` row would get the
*new* toolset stapled onto a run that never had it — silent corruption. Enabling
self-containment protects future data; deleting the pre-gate data protects the
past. `train/convert.py` now also prints a WARNING whenever it reattaches, so this
failure mode can't be silently re-introduced.

**Why this is a gate, not a nicety:** the agent-capability track (Phase 4)
*changes the toolset* (tool breadth: web, multi-edit, git, MCP). With non-self-
contained trajectories, the converter would staple the *new* toolset onto runs
captured with the *old* one — silently corrupting all prior training data. So
self-containment must be locked in **before** the first toolset change. The
converter is already forward-compatible, so this phase is small and isolated.

### Phase 4 — agent capabilities  (the parity backlog)
Build order TBD when we get here; rough priority is compaction → subagents first
(foundational + biggest multiplier). The full backlog:

1. **Context compaction** ✅ BUILT — summarize-and-continue via `src/context.py`
   (`ContextManager`) so long sessions / big repos don't blow the window. Honors the
   LOCKED capture-vs-context decision below: raw `turn` stream + `compaction` events
   in trajectory 0.3.0, safe-cut never orphans a tool message, converter view via
   `CODE_SFT_VIEW`. Dep-free test confirms raw history intact while as-sent compacts.
2. **Subagents / orchestration** ✅ BUILT — `spawn_agent` tool + `src/subagent.py`:
   a child agent with its own clean `ContextManager` and its OWN trajectory (linked
   by `parent_session_id` + `depth`, trajectory 0.4.0) runs in isolation and returns
   just its final answer. Depth capped by `CODE_MAX_SUBAGENT_DEPTH` (env), enforced
   at the tool. Nested runs are captured -> subagents multiply the dataset.
   **First toolset change since the Phase-3 gate, and it was safe** — adding
   `spawn_agent` didn't corrupt any prior trajectory because schemas are logged
   per-run. The gate paid off exactly as designed.
3. **Planning / decomposition** ✅ BUILT — `update_plan` tool (TodoWrite-style tracked
   checklist). The plan is the model's own action (captured in the trajectory as a tool
   call) and is PINNED into the live context (`ContextManager.set_pinned`) so it stays
   visible and survives compaction. No schema bump (uses existing `tool_call`). The pin
   is a context device only — plan content stays in the raw `turn` stream via the call.
4. **Interactivity & sessions** ✅ BUILT (v1) — multi-turn REPL (`python -m src`),
   `ask_user` clarifying-question tool (safe-degrades when no human is present),
   and `--resume <id>` that rehydrates a stopped session from its own trajectory
   (`src/session.py`; the capture-vs-context payoff — no separate state file).
   Trajectory 0.5.0 (`session_resume` record). Also shipped the **per-step
   converter** (one SFT row per agent action, with per-step meta). Deferred to a
   follow-up: true mid-task interruption (Ctrl-C steering — needs concurrency).
5. **Tool breadth** ✅ BUILT — opt-in `web_fetch`/`web_search` (gated by `CODE_ENABLE_WEB`,
   off by default to preserve data sovereignty; web_search is BYO via `CODE_SEARCH_URL`),
   and **MCP** (`src/mcp_client.py`): connect stdio MCP servers from `CODE_MCP_CONFIG`,
   their tools appear as `mcp__<server>__<tool>`. Introduced the DYNAMIC toolset
   (`src/toolset.py`: base + web + MCP, assembled per run) — the first time the toolset
   varies by config/connection, and the Phase-3 per-run schema logging makes that safe.
   git/multi-edit deliberately skipped (run_command covers them). Follow-ups: HTTP/SSE
   MCP transport (stdio only today); npx-based MCP servers need a custom Docker image
   (default image is python+git only); watch eval as the toolset grows (gpt-oss
   tool-calling degrades with too many tools).
6. **Granular permissions + hooks** — ✅ CORE BUILT (hooks = pass 2). Ported Claude
   Code's permission model into a real engine (`src/permissions.py`) gated at DISPATCH
   (`src/agent.py` calls `permissions.decide(...)` before every tool, captured as a
   `permission` record, trajectory 0.6.0 — subagent-safe because the decision binds to
   the running agent's own trajectory). Three parts: **modes** (`CODE_PERMISSION_MODE`:
   default/acceptEdits/plan/bypass), **rules** (`CODE_PERMISSIONS_CONFIG`: allow/ask/deny
   with `tool_name(pattern)` matchers — `deny` wins even under bypass), and a **workspace
   fence** (`CODE_ADD_DIRS`: file tools confined to cwd + granted dirs — closes the old
   `..`/absolute-path escape in `_abs`). Precedence + acceptance are pinned in
   `specs/0001-permissions.md` and checked by `scripts/check_permissions.py` (19/19, no
   model needed); eval stayed 13/13 (back-compat: unset mode derives from CODE_AUTO_APPROVE).
   Headless-safe by construction: ask/default BLOCK (never allow) with no human present.
   **Deferred to pass 2**: programmable `PreToolUse`/`PostToolUse` hooks (the engine
   exposes a single `decide()` seam so they wrap it without rework).
7. **Cross-session memory** — ✅ BUILT. Ported Claude Code's project-memory idea
   (`src/memory.py` + the `remember` tool): the agent appends durable notes about a
   repo to `<workspace>/.openagent/memory.md`, and `cli`/`session` load that file into
   the system prompt at the start of every later run — so it accumulates knowledge of
   *your* codebase instead of starting cold. Spec'd in `specs/0002-memory.md`, checked
   by `scripts/check_memory.py` (10/10). **Opt-in** (`CODE_MEMORY`, default off): it
   writes a file into the target repo and must stay off for eval — which kept the suite
   at 13/13. Memory lands in the logged system-prompt turn, so the Phase-3
   self-containment gate still holds (no schema bump, no new record). Subagents build
   without memory (stay lean). Follow-ups: memory summarization/pruning (it appends,
   `load` only caps on read), and auto-extraction from trajectories.
8. **Deep robustness / error recovery** — the unglamorous, ongoing layer. Landed so
   far: eval-harness per-task error isolation (one failure can't crash the run);
   **model-gateway retries** (`CODE_MODEL_RETRIES`) — transient errors + dropped
   tool calls (the flaky-worker signature) are retried with backoff, so an
   *intermittent* endpoint stays usable (retried glitches aren't logged); and
   **cold-start handling** for scale-to-zero serverless, copied from openagent-infra
   — every call uses a generous `CODE_REQUEST_TIMEOUT` (600s) so a spin-up isn't
   aborted, and a one-time `warm_up()` probe (`CODE_WARMUP` / `CODE_WARMUP_BUDGET`)
   waits until a real tool_call comes back before the first task, so the first real
   task never eats the cold start (a cold worker returns 200s with empty tool_calls
   until warm). The diagnosis: warm endpoint = 8/8 with zero retries; cold = dropped
   calls. Retries' short backoff (~14s) didn't cover a 30-60s cold start; the warm-up
   absorbs it once, up front — the automated form of "run the probe, then the eval."

~5 of these = a *foundational* agent; all 8 = broad agent-capability parity.

#### LOCKED design decision — capture vs. context (must hold from item 1 onward)

Today a single `messages` list is both (a) what we send the model and (b) what we
log as training data. They're identical *only because nothing compacts yet*.
**Context compaction (item 1) splits them**, and the split must be honored:

- **Live working context** — what we send the model each turn. Compactable
  (summary of older turns + recent tail). Lossy by design. Owned by a NEW
  subsystem (a `ContextManager`); `agent.py` talks to it instead of growing a raw
  list.
- **Durable raw history** — the complete, append-only record of every turn.
  Lossless, **never** compacted. The training source of truth.

Rules:
1. Compaction may shrink the model's context; it must **never** shrink what we log.
2. The trajectory records **both**: the **raw turn data** (full history,
   reconstructable by concatenation, decoupled from the live window) **and** the
   **as-sent context** per `model_call` (what the model actually saw, summaries
   included). Store both — you can't recover a view you threw away. The as-sent
   view is what lets us later train the model to *work well from compacted context*.
3. `train/convert.py` then **chooses** which view to flatten into SFT rows (raw vs.
   as-sent), rather than being stuck with whatever the live window happened to hold.

Why this is locked now: `trajectory.log_model_call` and `convert.to_row` currently
read the live `messages` object directly, so they are coupled to the context. That
coupling is invisible until item 1 makes raw ≠ context — so the decoupling is a
prerequisite *of* item 1, not a later cleanup.

---

## Sequencing principles (why this order)

- **Eval before the converter:** the eval batch teaches what good vs bad
  trajectories look like, so the converter is designed on real data, not guesses.
- **Converter before agent capabilities:** the converter is the data pipeline, so
  once it's closed every Phase-4 run *automatically* becomes training data — the
  agent track doubles as data generation from day one.
- **Self-containment gate between them:** because Phase 4 changes the toolset, and
  that's what breaks reattachment.

## Known follow-ups (parked, not lost)

- **Genuinely-hard eval tier** so the pass-rate can actually MOVE. The first harder
  tier (5 discriminating tasks) landed and sharpened the verifies, but gpt-oss-120b
  still clears the whole suite 13/13 — the ceiling isn't found yet. Need tasks the
  model fails on some of the time (subtle algorithmic bugs, larger refactors,
  adversarial/ambiguous specs), and/or tightened constraints (lower `CODE_MAX_STEPS`,
  forced compaction via low `CODE_COMPACT_AT_TOKENS`) to surface the breaking point.
- `Dockerfile` layer reorder: move volatile `CODE_*` ENV defaults below the
  apt/pip layers so config tweaks don't force a full rebuild.
- `prompts.py` polish: "copy indentation exactly from the file you read; don't
  guess tabs vs spaces" (the residual edit-fidelity wart; self-recovers today).
- Compaction tuning: `context.py` now has an "only apply if it shrinks" guard, but
  at a pathologically low budget it still *attempts* a summarize() every turn when
  the kept tail alone exceeds the budget. Add a pre-check (don't attempt when the
  summarizable portion is too small to help) + guard against recursive
  over-summarization. Only matters under big-repo / low-budget pressure; surfaced
  by the forced `CODE_COMPACT_AT_TOKENS=800` stress test.

## Status line

Phase 0 ✅ · Phase 1 ✅ (eval now 13 tasks, 13/13 — harder tier added, ceiling not yet found) · Phase 2 ✅ · Phase 3 ✅ · **Phase 4 — compaction ✅ + subagents ✅ + planning ✅ + interactivity ✅ + tool-breadth ✅ + cold-start handling ✅ + permissions Core ✅ + cross-session memory ✅. Remaining: permission hooks (#6 pass 2) + the always-open robustness/eval-ceiling tail**.
