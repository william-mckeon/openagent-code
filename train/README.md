# Training — closing the flywheel

> Sequencing for the whole project lives in [`ROADMAP.md`](../ROADMAP.md). The
> converter below is **Phase 2**; the self-containment **gate** (Phase 3) must
> land before any agent-capability / toolset change.

## The converter — `train/convert.py`

Turns captured trajectories into SFT rows. "Makes every run count."

```bash
python -m train.convert
```

What it does:
1. reads every `trajectories/**/*.jsonl`;
2. **filters** to trainable sessions — `outcome ∈ {success, completed}`,
   verification ok (when present), at least one tool call — and drops the rest
   (`no_action`, `protocol_stalled`, `verify_failed`, `max_steps`, `error`,
   `incomplete`) **with the reason counted**, never silently;
3. **flattens** each kept session into PER-STEP rows — one per agent action
   (`model_call`): `{messages: the prefix the model saw, completion: the action it
   took}` plus tool schemas. User/tool messages stay inside the prefix;
4. **writes** `train/dataset/sft.jsonl` and `train/dataset/report.json` (auditable
   counts: sessions kept, rows out, dropped-by-reason, schema source). `train/dataset/`
   is git-ignored — it carries the same code/prompts as the trajectories.

Row shape: `{ "messages": <prefix>, "completion": <agent action>, "tools": [...],
"meta": {session_id, step, outcome, view, depth, tools_called, all_ok, max_retry} }`.

**Forward-compatible tool schemas (the Phase-3 gate).** Native-mode trajectories
log only tool *names* (full schemas ride the API `tools` param), so today the
converter **reattaches** the current `src/tools.py` schemas. That is correct only
while the toolset is stable. Phase 3 logs the full schemas once in
`session_start` (`src/trajectory.py`) and re-runs the eval; the converter already
**prefers** that field and falls back to reattachment, so it picks up the richer,
self-contained data with no code change. This must happen **before** Phase 4
changes the toolset, or reattachment corrupts older trajectories. See `ROADMAP.md`.

---

## The training ladder (downstream of the converter)

You don't train until you have a few hundred good trajectories in `trajectories/`.
Then, in ascending cost/complexity (do not skip ahead):

### 1. SFT on winning trajectories  (start here)
Filter `trajectories/*.jsonl` to sessions with `outcome == "success"` (and clean
verification). Flatten each into (messages -> assistant action) training examples.
You are cloning your own harness's best behavior into the model.
Tooling: Unsloth or TRL + LoRA on a single GPU.

### 2. Rejection sampling / best-of-N distillation  (the workhorse)
For a task state, sample N completions, keep only those whose `verify` passed,
SFT on those. "Tests pass" is an objective filter you already produce via the
mandated verification step — this is the highest-leverage method for coding.

### 3. DPO on preference pairs
Build pairs from the same state: accepted-vs-rejected diff, or pass-vs-fail
completion. The `user_label` field (accept/reject) and `verification.ok` feed this.

### 4. RL (GRPO) with the verifiable reward
Reward = tests pass / spec met. Most powerful, most complex. Save it until 1–3
plateau, measured on `eval/`.

### Always
- **Eval gate:** never promote a model that doesn't beat the current one on `eval/`.
- **Hygiene:** scrub secrets/PII before storing or training; dedup; never train on
  your eval tasks (decontaminate). This pipeline is exactly where a data leak would
  happen — keep it clean from the first commit.

### Row unit: per agent step (not per conversation)
`convert.py` emits **one row per agent action** (`model_call`), not one row per
session: `{ "messages": <prefix the model saw>, "completion": <the action it took>,
"tools": [...], "meta": {session_id, step, outcome, view, depth, tools_called,
all_ok, max_retry} }`. User and tool messages live inside the prefix — they are
never their own target row (we don't train the model to speak as the user). This
per-step unit is what step-level filtering, rejection sampling, and DPO/RL operate
on, and the per-step `meta` (e.g. `max_retry`, `all_ok`) lets you weight or drop
individual steps. A multi-turn session simply yields more rows.

### The two views (chosen by `CODE_SFT_VIEW` — capture-vs-context decision, ROADMAP)
Each step's **prefix** is built one of two ways:
- **`raw`** (default) — the uncompacted history up to that step, reconstructed from
  the `turn` stream. The source of truth.
- **`as_sent`** — exactly what the model received that step (`model_call.request.messages`,
  possibly compacted). Use this to train the model to work well *from* compacted context.

For pre-0.3.0 trajectories there are no `turn` records, so `raw` falls back to the
as-sent prefix. Reasoning (`response.reasoning`) is kept in the trajectory but not
put in rows today — available if we later want reasoning-SFT.

### Subagents multiply the data (0.4.0)
Each `spawn_agent` call runs a child agent that writes its **own** trajectory,
linked to the parent by `parent_session_id` + `depth` (in `session_start`, surfaced
in each row's `meta`). So a single task can yield several trajectories — the
parent plus one per delegated subtask — and `convert.py` picks them all up. More
focused, self-contained training rows per run.
