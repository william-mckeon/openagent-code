# Training — closing the flywheel

> Sequencing for the whole project lives in [`ROADMAP.md`](../ROADMAP.md). The
> converter below is **Phase 2**; the self-containment **gate** (Phase 3) must
> land before any agent-capability / toolset change.

## Capture the corpus — `train/capture.py`  (Stage 4)

The converter only makes rows from runs that already happened. `capture.py` is what
**spins the flywheel**: it points the teacher (whatever `.env` selects — gpt-oss-120b on
Bedrock) at the diverse training pool in `train/tasks/*.yaml` (18 tasks to start —
implement / fix-a-bug / add-a-feature across string, data-structure, parsing, math) and
captures every run. Add more tasks freely; they must stay distinct from `eval/`.

```bash
python -m train.capture            # one pass over train/tasks/
python -m train.capture --repeat 3 # N passes (temperature>0 -> varied trajectories)
```

Each task runs in a throwaway sandbox with its `verify` command, and the trajectory lands
in **`trajectories/corpus/`** — deliberately separate from `trajectories/eval/`. That is the
**train/eval firewall**: `train/tasks/` (corpus, trained on) and `eval/tasks/` + `eval/agentic/`
(the held-out gate, judged) are different task sets in different trajectory dirs, and
`convert.py` excludes the gate dir. Keep them disjoint — a task that appears in both is a
data leak that quietly inflates your gate. Then run `python -m train.convert` to curate.

## The converter — `train/convert.py`

Turns captured trajectories into SFT rows. "Makes every run count."

```bash
python -m train.convert
```

What it does:
1. reads every `trajectories/**/*.jsonl` **except the held-out eval gate**
   (`trajectories/eval/` is excluded — training on it would be teaching to the test;
   the count of excluded gate trajectories is reported as `excluded_eval_gate`);
2. **filters** to trainable sessions — `outcome ∈ {success, completed}`,
   verification ok (when present), at least one tool call, AND a **behavior gate**
   (drops `refusal` runs — "narrow the scope" deflections — via `eval/rubric.py`, so
   we never train the model to refuse) — dropping the rest (`no_action`,
   `protocol_stalled`, `verify_failed`, `max_steps`, `error`, `incomplete`,
   `refusal`) **with the reason counted**, never silently;
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

## The trainer — `train/sft.py`  (Stage 5)

Turns the curated `sft.jsonl` rows into a **student checkpoint** by LoRA-SFT. The one real
code build in the distillation plan; everything else is the reusable harness.

```bash
# smoke: tiny model + a few steps — proves the pipeline end to end (even CPU), no big GPU
python -m train.sft --smoke

# real Tier-1 run on a GPU box (install the extra there first)
pip install -e ".[train]"          # + a CUDA torch build for that machine
python -m train.sft --model openai/gpt-oss-20b --epochs 1 --out train/checkpoints/student --load-4bit
```

### Run it in Docker (recommended — clean Linux env on your local GPU)

The training stack (torch+CUDA, bitsandbytes) is brittle to install on a host, and Windows
can block the numpy DLLs. So train in a container instead — a clean Linux env on your **local
NVIDIA GPU** (no RunPod, no cloud). The image (`docker/train/Dockerfile`, CUDA 12.8 / cu128,
Blackwell-ready) and two compose services are provided. `train/`, the dataset, checkpoints, and
`trajectories/` are **mounted**, so editing a task or `sft.py` needs no rebuild.

```bash
docker compose build train                      # build once (heavy: CUDA + torch + the [train] extra)

# prove the loop on CPU (tiny model, few steps) — no GPU needed:
docker compose run --rm train-smoke

# the real run on your GPU (NVIDIA Container Toolkit / Docker Desktop+WSL2 required):
docker compose run --rm train \
    python -m train.sft --model openai/gpt-oss-20b --epochs 1 --load-4bit

# long run, detached (survives terminal close), then follow the logs:
docker compose run -d --name oac_train train \
    python -m train.sft --model openai/gpt-oss-20b --epochs 1 --load-4bit
docker logs -f oac_train
```

Windows PowerShell uses the same commands (Docker Desktop + WSL2 passes the GPU through).
Checkpoints land in the mounted `train/checkpoints/` on the host. Adjust the CUDA/torch
versions in `docker/train/Dockerfile` if your card isn't Blackwell (an Ampere/Ada card can use a
cu124 wheel).

**The data bridge** (`build_example`): each row's `messages + completion` is rendered through
the tokenizer's chat template (with the row's `tools`), and the **prompt is masked (-100)** so
loss falls only on the agent's ACTION — we clone the *decisions*, not the user/tool text
(completion-only SFT). Tokenizers whose template can't render tool-calls fall back to a flat-text
rendering, so the smoke path runs on any model. Heavy deps are imported lazily, so the file
imports without the `[train]` extra; it just won't *train* until you install it on a GPU box.

## Stage 6 — merge → serve → swap → gate

Once `train/sft.py` has produced an adapter, turn it into a deployed, gated student:

```bash
# 1. MERGE the LoRA adapter into the base -> a standalone model dir vLLM can serve
docker compose run --rm train python -m train.merge --adapter train/checkpoints/student
#    -> train/checkpoints/student-merged

# 2. SERVE it locally on vLLM (OpenAI-compatible, native tool-calls), on your GPU
docker compose up serve          # http://localhost:8000/v1, served-model-name "student"

# 3. SWAP it into the agent — the one-line boundary (in .env):
#       CODE_MODEL=openai/student
#       CODE_API_BASE=http://localhost:8000/v1
#       CODE_API_KEY=EMPTY
#    (add CODE_TOOL_MODE=json if you serve without a native tool-call parser)

# 4. GATE: run the eval suite against base AND student; promote only if student >= base
#    on BOTH verify pass-rate and behavior score.
python -m eval.compare \
    --base-model    openai/Qwen2.5-3B-Instruct --base-api-base    http://localhost:8001/v1 \
    --student-model openai/student             --student-api-base http://localhost:8000/v1
```

`eval.compare` prints a base-vs-student table + a **PROMOTE / KEEP BASE** verdict (exit 0 = promote).
Only swap the student into your daily `.env` if it passes. The eval suite stays held-out from the
training corpus (`convert.py` firewalls `trajectories/eval/`), so this gate is honest.

> **Blackwell (RTX 50) serving note.** vLLM needs a cu128/sm_120 build; if the pinned
> `vllm/vllm-openai` tag won't run on the 5080, bump `VLLM_TAG` in `docker/serve/Dockerfile`,
> or fall back to a transformers-based server from the `docker/train` image + `CODE_TOOL_MODE=json`.

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

### Measuring agentic *behavior*, not just correctness (`eval/rubric.py`, specs/0004)
The verify eval answers "did the code end up correct?" — it is blind to *agentic
quality* (review depth, grounding, refusing-vs-doing). `eval/agentic/*.yaml` are
review/investigate tasks scored by a deterministic **rubric** over the trajectory
(distinct files read, no "narrow the scope" refusal, finished with a real answer,
didn't over-ask). `python -m eval.harness` now prints a `behavior:` score beside the
verify pass-rate. This is the **signal the flywheel was missing**: it lets `convert.py`
*select* good behavior (the refusal gate above is the first use) instead of us
hand-patching the system prompt — the pivot from harness-code to training. v1 is
heuristic; an LLM-judge is the follow-up.

### Always
- **Eval gate:** never promote a model that doesn't beat the current one on `eval/`
  — on **both** the verify pass-rate AND the agentic behavior score.
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
