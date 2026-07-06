# 0006 — SageMaker training substrate (cloud LoRA-SFT)

> Run the distillation trainer (`train/sft.py`) on AWS SageMaker instead of a local GPU/Docker.
> Extends the flywheel's Stage 5/6 (specs/0005) with a cloud execution path; changes nothing
> about *what* is trained, only *where* it runs.

## Goal

Give openagent-code a clean, cheap, no-Docker way to run the LoRA-SFT job in the cloud, so
training doesn't depend on the host's local GPU stack. The **same** `train/sft.py` runs locally
and on SageMaker — one trainer, two substrates.

## Why

Local training on Windows/Blackwell (RTX 5080, `sm_120`) means fighting Docker + the
torch/peft/bitsandbytes/CUDA-wheel clash — the whole reason `docker/train` exists. AWS SageMaker
**Script Mode** sidesteps all of it: it runs the entry script in AWS's prebuilt PyTorch container
on a clean Linux GPU — **no Docker image to build, no ECR, no wheel roulette** — and is
spot-capable (~50–70% off). The pattern is proven in the sibling **Arcus** project
(`models/Alpha base/launch.py`), which pretrains a from-scratch model this way; Arcus is also
openagent-code's parked Tier-3 student, so putting both on one SageMaker substrate is deliberate.

## Design

- **Script Mode, no Docker/ECR.** `train/launch_sm.py` submits a `sagemaker.pytorch.PyTorch`
  estimator against the prebuilt DLC (`framework_version=2.4`, `py_version=py311`).
- **Staged source dir.** `stage_source()` builds `.sm_src/` = `train/sft.py` (entry point) +
  `train/requirements-sm.txt` copied to `requirements.txt` (SageMaker auto-installs it). The
  requirements are **torch-free** — the container ships torch matched to its CUDA.
- **Same trainer, both places.** `train/sft.py` reads `SM_CHANNEL_TRAIN` (the S3 data channel,
  mounted as a dir) for `--data` and `SM_MODEL_DIR` (`/opt/ml/model`) for `--out` when SageMaker
  sets them; off SageMaker the local defaults stand. `store_true` flags (`--smoke`, `--load-4bit`)
  became `nargs="?"` so SageMaker's `--key value` hyperparameter passing works. (Both are the
  exact patterns Arcus's `scripts/train_arcus.py` uses — `SM_CHANNEL_TRAIN or args.shards`.)
- **Data bundled; output via S3.** The tiny `sft.jsonl` is BUNDLED into `source_dir` (it rides to
  SageMaker's default bucket with the code) — no data bucket to create, no separate upload; it
  lands next to `sft.py` in `/opt/ml/code`, where `sft.py` finds it. The trained LoRA adapter comes
  back as SageMaker's model artifact (`model.tar.gz`); the launcher downloads + unpacks it into
  `train/checkpoints/student` so `train/merge.py` finds it unchanged. (For a much larger corpus
  later, switch to an S3 data channel; bundling is the right call while the dataset is small.)
- **Secrets from the shell.** `HF_TOKEN` is forwarded to the job only if set (private base models),
  never committed. Bucket/role via env (`CODE_SM_BUCKET` / `CODE_SM_ROLE`).

## Keep vs. simplify (vs. Arcus's pretraining launcher)

Arcus's launcher is tuned for a weeks-long, ~120GB, interrupt-prone **pretraining**. openagent-code's
LoRA-SFT is a **short, small-data** job, so:

| Arcus does | openagent-code |
|---|---|
| Script Mode, prebuilt PyTorch DLC, no Docker | **Keep** — the whole point |
| `SM_CHANNEL_TRAIN` / `SM_MODEL_DIR` env-aware entry (local + cloud) | **Keep** |
| `HF_TOKEN`-from-shell, role/bucket via env, torch-free reqs | **Keep** |
| Managed spot | **Keep** (cost) |
| Resume-from-S3-checkpoint after a spot kill | **Drop** — a short job just re-runs |
| FastFile streaming of ~20GB | **Drop** — dataset is MBs; upload it |
| L40S 48GB, 12B-token budgets | **Resize** — `ml.g5.xlarge` (24GB) for a 3B LoRA |
| Full `model.safetensors` → HF | **Adapt** — LoRA adapter → `/opt/ml/model` → S3 |

## Files

- **NEW** `train/launch_sm.py` — the estimator/submitter (stage source, upload data, fit, pull adapter).
- **NEW** `train/requirements-sm.txt` — torch-free container deps.
- **NEW** `specs/0006-sagemaker-training.md` — this spec.
- **EDIT** `train/sft.py` — `SM_CHANNEL_TRAIN`/`SM_MODEL_DIR` awareness; `nargs="?"` flags.
- **EDIT** `pyproject.toml` — `[sagemaker]` extra (`sagemaker`, `boto3`).
- **EDIT** `.env.example` — SageMaker block + the IAM-vs-Bedrock-auth caveat.
- **EDIT** `.gitignore` — `.sm_src/`, `.sm_out/`.
- **EDIT** `train/README.md`, `ROADMAP.md` — document the cloud path.
- **UNCHANGED** `train/merge.py` — operates on the local adapter dir the launcher populates.
- **KEPT** `docker/train` + the `train`/`train-smoke` compose services — the local validation bench.

## Acceptance (checkable)

- [ ] `pip install -e ".[sagemaker]"` installs the launcher deps.
- [ ] `python -m train.launch_sm` errors clearly when `CODE_SM_BUCKET`/`CODE_SM_ROLE` are unset
      or `sft.jsonl` is missing (no half-submitted job).
- [ ] A **smoke** submit (tiny model, spot) runs end to end on SageMaker and returns an adapter
      to `train/checkpoints/student` that `train/merge.py` merges without changes.
- [ ] `train/sft.py` still runs **locally** (`--smoke`) and in **Docker** unchanged — the env-var
      awareness is inert when `SM_*` is unset.
- [ ] The trained student passes the existing gate (`eval/compare.py`) before any swap.

## Non-goals (this pass)

- **Replacing the local Docker path.** It stays as the cheap validation bench (laptop-vs-cloud split).
- **Spot resume / checkpoint-to-S3.** Not worth it for a short job; a reclaimed run just re-runs.
- **A SageMaker *serving* endpoint.** Serving stays the local vLLM `serve` service (Stage 6); the
  merged student is portable. A SageMaker endpoint is a possible later addition, not here.
- **Running `convert` on SageMaker.** Curation is fast, offline, CPU — it stays local; only the
  GPU SFT goes to the cloud.

## Notes

- **Two AWS auths, same account.** The Bedrock teacher uses a bearer token
  (`AWS_BEARER_TOKEN_BEDROCK`); SageMaker needs standard IAM creds (`aws configure`, `AKIA…`) plus
  the execution role. Don't conflate them — Arcus's runbook flags the same trap.
- **Size the instance to the student.** 3B LoRA → 24GB (`ml.g5.xlarge`); `gpt-oss-20b` needs 4-bit
  (`--load_4bit`) + a bigger card (`ml.g6e.xlarge`). Full-precision 20B won't fit 24GB.
