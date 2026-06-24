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

Capture, outcome-labeling, the converter, and eval are all done. **The pivot (2026-06):
the harness (axis 1) is essentially built; the remaining failures — shallow reviews,
refusals, overclaiming, weak judgment — are model-quality (axis 2), and the ROADMAP says
those close via the FLYWHEEL (train), not more prompt rules.** We had been patching them in
the system prompt; that whack-a-mole is now frozen as a floor. The blocker was that the
eval only scored binary correctness and was blind to agentic quality — so the flywheel had
no signal to select on. That's now built: `eval/rubric.py` + `eval/agentic/*.yaml` score
behavior (depth / no-refusal / completion) deterministically, `eval.harness` reports it
beside the pass-rate, and `train/convert.py` gates on it (drops `refusal` runs). Next:
curate a few hundred good trajectories and run the first SFT pass (`train/sft.py`) — the
first time the model itself improves, not the harness. See specs/0004-agentic-evals.md.

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
   Later, a live REPL log review surfaced two more, now fixed: (a) a **mid-session**
   dropped tool call (the worker going cold *again* during a session) used to exhaust the
   short backoff and end a turn in `(no output)` — now a drop re-runs `warm_up()` to
   re-absorb the cold start instead of failing; and (b) **compaction lost the live thread**
   (after summarizing, the agent forgot it had just finished a task and re-asked) — fixed
   by the `SUMMARIZE_PROMPT` preserving the most-recent request + last action, plus higher
   `CODE_COMPACT_AT_TOKENS` (16000) / `CODE_COMPACT_KEEP_RECENT` (8) defaults. The same log
   also drove the **grounding** prompt rules (no reviewing the wrong folder, no speculating
   about unread files) — see specs/0003-host-access.md.
   **Cold-start, settled:** a review of OpenAgent-infra found it points at the SAME RunPod
   serverless endpoint (`ryebshj6yomwei`) — there is no warm/alternate endpoint and no
   warm-up/keep-alive in infra; its only trick is a **600s** read timeout (patiently waits
   the spin-up). openagent-code's `CODE_WARMUP_BUDGET` was 120s, which gave up early and
   then thrashed (give-up → real call drops → re-warm, looping for minutes). Fixed by
   matching infra's patience: `CODE_WARMUP_BUDGET` default → **600** (one long wait, no
   thrash). The *real* cure remains a min-active worker on the RunPod endpoint (dashboard,
   no code) so it never scales to zero. A second log review also tightened **review
   discipline** in the prompt: don't refuse a broad review (map structure → read key files
   → overview), don't overclaim coverage (only describe files actually opened), and "this
   project" means the workspace, not a folder discussed earlier.

9. **Review at scale + scope safety (the orchestrator)** ✅ BUILT (2026-06-22/23, commits
   `8a418cf` → `8109e85`). Driven by repeated live "review the whole project" failures that each
   died at a *different* wall (out of steps → half-done → context overflow → 131k `BadRequestError`).
   The fix was **deterministic orchestration, not more prompting**:
   - **`review_repo` + `src/orchestrator.py`** — the HARNESS splits the repo and reviews each area in
     a bounded child, returning summaries the lead synthesizes, so the lead never holds the whole repo
     (can't overflow). The agentic part: the model proposes the `areas` plan; a `_balance_plan`
     guardrail collapses root-file spam and **guarantees every top-level folder (esp. `src/`) is reviewed**.
   - **`tree`** (one-call map), **synthesis-on-`max_steps`**, **non-interactive children** (can't hijack
     the REPL), per-message cap + **non-retryable** BadRequest/context-overflow (overflow safety),
     `search`/`glob` aliases, `CODE_MAX_STEPS` 25→50.
   - **Read-only reviews + secret guardrails** — a review REPORTS, never edits/creates/runs or touches
     `.env`; `.env` edits are `ask`-gated; no confabulation. (Added after the agent "redacted" the live
     Bedrock token in `.env` during a review and broke auth.)

**Deferred — Phase 4 is NOT fully closed (don't forget these):**
- **Hooks** (item 6, pass 2) — programmable `PreToolUse`/`PostToolUse`. The deterministic enforcement
  layer; the `.env`-overwrite incident is its motivating case. The engine already exposes one `decide()`
  seam for it. **The main open Phase-4 item.**
- **Mid-task interrupt** (item 4) — Ctrl-C steering (needs concurrency).
- **MCP transports** (item 5) — HTTP/SSE (stdio only today); npx servers need a custom Docker image.
- **Memory pruning / auto-extraction** (item 7).

~5 of these = a *foundational* agent; all 8 = broad agent-capability parity. Items 1-8 cores + item 9
are built; **hooks are the notable gap.**

### Phase 5 — the distillation flywheel (make the MODEL)  → specs/0005-distillation.md
The harness is built; this phase produces the *model*. Use the flywheel + a strong teacher
(**gpt-oss-120b on Amazon Bedrock via boto3/IAM**) to **distill a smaller, deployable student**
(any *pretrained* model — Tier 1 = gpt-oss-20b, Tier 2 = a small instruct model; from-scratch
is a parked Tier 3) that learns *your* agentic-coding distribution, then swap it in behind the
same `CODE_API_BASE` boundary. **Documented in full** in `specs/0005-distillation.md`; **built in
gated stages**, not one blob:

1. **Document** (this + the spec). ✅
2. **Bedrock teacher** — boto3 config + one provider-aware `reasoning_effort` tweak in
   `src/model.py` + a `[bedrock]` extra. Gate: `check_native_toolcalls` `[OK]` + eval 13/13 on
   120b/Bedrock. *Standalone win: ends the RunPod cold-start/500 pain, 128k window.* **✅ PASSED
   (2026-06-20): 13/13 (100%), behavior 1.00**, auth via a Bedrock API-key bearer token in
   `us-east-1`. The gate also surfaced + fixed two flywheel-critical harness bugs (grep glob in
   `tools.py`, dropped `reasoning_content` in `planner.py` that made gpt-oss loop on multi-step tasks).
3. **Harden the eval** — a discriminating tier so the promotion gate can actually move off 100%.
   **✅ BUILT (2026-06-21):** findings-based behavior scoring (`must_mention`), task tiers
   (smoke/core/hard) + per-tier reporting, a `hard` tier (`security_audit`, tricky verify
   tasks), delegation-aware depth, gate verdict. Discriminates by construction; plus a
   calibrated edge task (`security_audit_hard`) the 120b fails ~1/3 of runs (a missed ReDoS),
   so the teacher no longer reliably reads 100%. A weaker student reveals the full gap (Stage 6).
4. **Capture + curate** the corpus from the 120b teacher. **✅ PIPELINE BUILT (2026-06-22):**
   `train/tasks/` (training pool, separate from the eval gate) + `train/capture.py` (teacher →
   `trajectories/corpus/`) + a train/eval **firewall** in `convert.py` (excludes the gate). First
   batch: 8/8 captured, 319 gate trajectories excluded, 1,131 clean rows. Scale via more tasks + `--repeat`.
5. **`train/sft.py`** — the trainer + data bridge (the one real build; LoRA, single GPU).
   **✅ BUILT (2026-06-23):** completion-only LoRA-SFT; the `build_example` data bridge masks the
   prompt so loss is on the agent's action; heavy deps lazy-imported; a `--smoke` path proves the
   pipeline anywhere (verified: masking + graceful guard). Remaining = the real Tier-1 GPU run.
6. **Distill → eval-gate → serve (vLLM) → swap** (one `.env` line). Gate: student ≥ base. ❌
7. **Close the loop** — deployed student generates more trajectories → retrain. ❌

**◆ WHERE WE STAND (2026-06-23).** The RunPod→Bedrock migration is DONE and the harness is robust +
agentic; the flywheel is HALF-built. The whole effort = a **12-part upgrade**:
*Migration:* (1) Bedrock teacher · (2) message-shaping + error classification · (3) turn rollback ·
(4) cross-platform + quiet startup. *Harness bugs it exposed:* (5) grep glob · (6) reasoning_content
preservation. *Agentic reach:* (7) decomposition + tree + synthesis · (8) the review_repo orchestrator.
*Safety:* (9) read-only reviews + secret guardrails. *Flywheel:* (10) discriminating eval · (11) corpus
capture + train/eval firewall · (12) the gated 7-stage plan + docs. **Parts 1-12 are built/committed;
flywheel Stages 1-5 ✅ BUILT** (teacher 13/13, eval discriminates, corpus pipeline + 18-task pool,
and `train/sft.py` — data bridge + LoRA-SFT + `--smoke`, verified). **Execution runs LOCALLY on the
GPU, in Docker** — `docker/train/Dockerfile` (CUDA 12.8 / cu128, Blackwell-ready, ported from the proven
boenet pattern) + `train`/`train-smoke` compose services give a clean Linux env that sidesteps the host's
Windows DLL block + torch/peft version clash. Not RunPod, not Bedrock (both are inference, not training).
**Next: `docker compose build train` → `train-smoke` (CPU proof) → the real `--gpus` run on gpt-oss-20b**,
then Stage 6 (distill → gate → serve → swap) and Stage 7 (close the loop). Loose ends: grow the corpus,
and the deferred Phase-4 **hooks** pass.

The only genuine code is `train/sft.py` + the one `model.py` reasoning tweak; the whole `src/`
harness is otherwise the reusable body the student steps into. Caveats (in the spec): distillation
caps the student at the teacher (RL surpasses it later), small students tool-call worse, and the
eval MUST discriminate first or the gate is blind. boenet/from-scratch is explicitly out of scope
here — the student is "any pretrained model."

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

Phase 0 ✅ · Phase 1 ✅ (eval now 13 tasks, 13/13 — harder tier added, ceiling not yet found) · Phase 2 ✅ · Phase 3 ✅ · **Phase 4 — compaction ✅ + subagents ✅ + planning ✅ + interactivity ✅ + tool-breadth ✅ + cold-start handling ✅ + permissions Core ✅ + cross-session memory ✅. Remaining: permission hooks (#6 pass 2) + the always-open robustness/eval-ceiling tail** · **Phase 5 — distillation flywheel (gpt-oss-120b/Bedrock teacher → distilled student): Stage 1 (documented) ✅; Stages 2-7 staged (specs/0005)**.
