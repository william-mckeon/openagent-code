# Distillation flywheel — gpt-oss-120b → a smaller deployable student (Phase 5)

The harness is built; now make the MODEL. Use openagent-code's flywheel plus a strong
teacher (gpt-oss-120b) to **distill a smaller, cheaper, deployable student** that learns
*your* agentic-coding distribution — then swap it in behind the same `CODE_API_BASE`
boundary. The end state: a model you own, trained on your captured work, running your
agent. This spec documents the **whole vision (current + future)**; the build executes in
**gated stages** (below), not one blob.

## Locked decisions

- **Teacher: gpt-oss-120b on Amazon Bedrock, via boto3 / IAM.** Managed → no scale-to-zero
  cold starts, 128k context, standard AWS auth (IAM roles/SSO, no static key to rotate).
  This is also the immediate cure for the RunPod cold-start/500 pain — valuable on its own.
- **Student: ANY *pretrained* model (NOT from-scratch).** Dropping from-scratch is the
  decision that makes this feasible — it deletes pretraining, tokenizer surgery, and
  teaching tool-calling from zero. Tiers:
  - **Tier 1 (do first): gpt-oss-20b.** Same family as the teacher → **same tokenizer/tool
    format** → enables both response-based AND logit-KL distillation, already tool-calls,
    already served, runs in the harness today. Lowest risk; proves the loop end-to-end.
  - **Tier 2: a small instruct model** (Qwen2.5-1.5B/3B, Llama-3.2-3B). Smaller/cheaper,
    response-based distillation only (different tokenizer). More "your own model."
  - **Tier 3 (future, documented not built): a from-scratch model** (its own pretrain +
    tokenizer + serving shim + tool-calling). The long road; parked.
- **Method:** **response-based (sequence-level) SFT now** — universal, works for any
  student. **Logit-KL (soft-label) distillation** is a same-family (Tier 1) follow-up.
- **Tool-calling is a learned SFT skill** — the captured trajectories ARE the curriculum.
  Pretrained students already tool-call; `CODE_TOOL_MODE=json` is the no-native fallback.
- **Serving:** vLLM for standard-arch students; swap in via the `CODE_API_BASE` one-liner.

## The pipeline — built vs. the build

| Stage of the loop | What happens | Status |
|---|---|---|
| Set teacher | gpt-oss-120b on Bedrock (boto3) | config + 1 tiny code change |
| Capture | run the agent → trajectories (`src/trajectory.py`) | ✅ built |
| Curate | `train/convert.py` → SFT rows, behavior-gated | ✅ built |
| Train | SFT/LoRA the student | ❌ **`train/sft.py`** |
| Eval-gate | `eval.harness` verify + behavior; promote only if it beats base | ✅ built (needs a harder tier) |
| Serve + swap | vLLM + `CODE_API_BASE` line | ✅ boundary built |
| Loop | deployed student generates more trajectories → retrain | ✅ the flywheel |

**The only genuine code build is `train/sft.py`** (+ a one-line `model.py` reasoning tweak
for the boto3 path). The whole `src/` harness is otherwise untouched.

## Stages (execution order — each PASSES its gate before the next)

1. **Document (this spec + ROADMAP).** Gate: the recipe, tiers, and file manifest are written.
2. **Bedrock teacher. ✅ PASSED (2026-06-20).** boto3 config + the `model.py` reasoning tweak +
   `[bedrock]` extra. Auth was a **Bedrock long-term API key** (bearer token via
   `AWS_BEARER_TOKEN_BEDROCK`), not access-key/secret; `us-east-1`, plain model id
   `openai.gpt-oss-120b-1:0` (no inference profile needed).
   Gate MET: `check_native_toolcalls` `[OK]`, `reasoning_effort=high` reaches the teacher, and
   eval **13/13 (100%)**, behavior **1.00**.
   *Surfaced + fixed two harness bugs the teacher's clean reasoning made legible (both corrupt
   trajectory quality, so flywheel-critical):* (a) `grep` matched its `glob` filter against the
   bare filename, so every `**/*.py` returned "(no matches)" → agent flew blind (`src/tools.py`);
   (b) the assistant's `reasoning_content` was dropped from history, so gpt-oss lost its plan
   between tool calls and looped on multi-step tasks (`src/planner.py`). `multi_file_rename`:
   failing 4/4 → passing 3/3.
   *(Standalone payoff: escapes the RunPod cold-start/500 pain immediately.)*
3. **Harden the eval. ✅ BUILT (2026-06-21).** Findings-based behavior scoring
   (`rubric.must_mention`), task `tier`s (smoke/core/hard) with per-tier reporting, a `hard`
   tier (e.g. `security_audit` with subtle planted vulns), delegation-aware depth, and a gate
   verdict line. The eval now discriminates BY CONSTRUCTION (a shallow review scores below a
   sharp one — unit-proven), and lands a **calibrated edge-of-competence task**:
   `security_audit_hard` (a ReDoS-prone regex among obvious vulns) — the 120b misses the ReDoS
   ~1 in 3 runs (behavior 0.80 / 1.00 / 1.00), so the teacher no longer reliably reads 100%.
   A frontier teacher can't be *deterministically* stumped on fair tasks; this sits at its edge,
   where a promotion gate belongs. A weaker student misses it (and more) far more often — the
   real discrimination, validated at Stage 6.
   *(Prerequisite for trusting anything downstream — a blind gate makes distillation meaningless.)*
4. **Capture + curate the corpus. ✅ PIPELINE BUILT (2026-06-22).** A separate training task
   pool (`train/tasks/*.yaml`, distinct from the eval gate) + `train/capture.py` (runs the
   teacher → `trajectories/corpus/`) + the **train/eval firewall** in `convert.py` (excludes
   `trajectories/eval/` — no teaching to the test). First batch: 8/8 tasks captured, 319 gate
   trajectories firewalled out, **1,131 clean behavior-gated rows**. *Scaling up = more
   `train/tasks/` + `--repeat` passes; the gate (clean dataset of N rows) is met and grows.*
5. **The trainer — `train/sft.py`.** Gate: SFT a tiny student → checkpoint → eval runs on it.
6. **Distill → gate → serve → swap.** Tier-1 student. Gate: distilled student ≥ base on the
   (now-discriminating) eval, served via vLLM, swapped in with one `.env` line.
7. **Close the flywheel.** Deployed student → more trajectories → recapture → retrain. Ongoing.

## Acceptance (the loop is closed)

- [x] gpt-oss-120b reachable on Bedrock via boto3 (Bedrock API-key bearer token), `check_native_toolcalls` `[OK]`.
- [x] `reasoning_effort=high` reaches the Bedrock teacher (provider-aware passing).
- [x] The eval discriminates BY CONSTRUCTION (findings checks + hard tier; a shallow review
      scores below a sharp one). The 120b teacher saturates it — a weaker student will reveal the gap.
- [x] A captured + curated corpus exists (`train/dataset/sft.jsonl`, behavior-gated, eval-decontaminated).
      Pipeline: `train/tasks/` → `train/capture.py` → `trajectories/corpus/` → `convert.py` (firewalls the gate).
- [ ] `train/sft.py` turns those rows into a student checkpoint (LoRA on a single GPU).
- [ ] The distilled student, served via vLLM and swapped in (`CODE_API_BASE`), **meets or beats
      the base student** on the eval — verify pass-rate AND behavior score.
- [x] The agent is provider-agnostic. (Stage 2 touched 3 `src/` files, not the 1 planned: the
      `model.py` reasoning tweak as designed, **plus** two correctness fixes the gate uncovered —
      `tools.py` grep-glob and `planner.py` reasoning-preservation. Neither is provider-specific.)

## File manifest (current + future)

**ADD:** `specs/0005-distillation.md` (this) · `train/sft.py` (trainer + data bridge) ·
harder eval tasks (`eval/tasks/*.yaml`, `eval/agentic/*.yaml`).
**UPDATE:** `src/model.py` (provider-aware `reasoning_effort`) · `src/tools.py` (grep glob-vs-path
fix) · `src/planner.py` (preserve `reasoning_content` across tool calls) — the three `src/` edits ·
`pyproject.toml` (`[train]` + `[bedrock]` extras) · `.env` + `.env.example` (Bedrock-teacher
and student-serving profiles; `CODE_MAX_STEPS` 15→30 for the 128k teacher) · `train/README.md`
(trainer + vLLM serve runbook + ladder) · `ROADMAP.md` · `README.md` · `docs/DATASHEET.md`.
**VERIFY-NO-CHANGE:** `train/convert.py`, `src/config.py`, `eval/harness.py`, `eval/rubric.py`.
**DELETE:** none.

## Non-goals (this phase)

- **From-scratch / boenet student (Tier 3)** — documented, not built. Needs its own pretrain
  (~100GB ≈ 25-30B tokens for a 1.3B model), tokenizer, serving shim, and tool-calling
  from zero. A later phase.
- **Logit-KL (soft-label) distillation** — a Tier-1 follow-up after response-based SFT is proven.
- **RL beyond the teacher** (GRPO on the verify reward) — the rung that *surpasses* the
  teacher; comes after SFT plateaus.
- **Distributed / multi-GPU training** — start single-GPU LoRA on a small student.

## Honest caveats / risks

- **Distillation caps the student at the teacher** on the captured distribution — a distilled
  20b gets better at *your* tasks but won't exceed the 120b. Surpassing the teacher needs RL.
- **Teacher-size ceiling:** you can't distill a student *bigger/better* than the teacher.
  gpt-oss-120b → a smaller student ✅; a frontier-scale student would need pretraining + RL,
  not distillation alone.
- **Small students tool-call worse.** We watched a 20b drop/confabulate tool calls; a 1.3B
  (Tier 2) will be shakier. `json` mode is more forgiving; scale buys reliability.
- **The eval must discriminate** (Stage 3) or the promotion gate is blind — the single most
  important prerequisite for trusting the result.
- **Data volume:** a handful of trajectories won't move a model. The flywheel has to actually
  spin — run the 120b across many diverse tasks, behavior-gate hard.

## Notes

- **The boto3 reasoning wrinkle:** our `reasoning_effort` is sent via `extra_body` (OpenAI
  path); the `bedrock/` provider needs it as the top-level param (→ `additionalModelRequestFields`),
  and recent LiteLLM versions fixed earlier bugs there. Hence the one provider-aware `model.py` edit.
- **The harness is the reusable body.** Capture, curate, behavior-gate, eval, the swappable
  serving boundary, and json-mode tool fallback are all done. The student steps into a body
  that's already built — "import" = SFT it + serve it, then the swap is one config line.
- **Why this is the whole thesis closing:** openagent-code (capture the teacher doing real
  agentic work) → a distilled student (your model, your distribution) → swapped back into the
  same agent → which generates more data. Sovereignty + the training flywheel, end to end.
