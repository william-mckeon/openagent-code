"""
train/launch_sm.py

Submit the openagent-code LoRA-SFT run to AWS SageMaker as a (managed-spot) training job.

Why: local GPU training on Windows/Blackwell means fighting Docker + the torch/peft/sm_120
clash. SageMaker Script Mode runs train/sft.py in AWS's prebuilt PyTorch container on a clean
Linux GPU — no Docker, no ECR, no CUDA-wheel roulette. The SAME train/sft.py runs here and
locally: it reads SM_CHANNEL_TRAIN / SM_MODEL_DIR when SageMaker sets them, else the local
--data / --out defaults stand.

Adapted from the Arcus launcher (models/Alpha base/launch.py). LoRA-SFT is a SHORT, small-data
job, so we KEEP the good parts (Script Mode, spot, secrets-from-shell) and DROP the pretraining-
scale machinery (FastFile streaming, resume-from-S3-checkpoint). The tiny dataset is BUNDLED into
source_dir (it rides to SageMaker's DEFAULT bucket with the code) — no data bucket to create, no
separate upload.

  # AWS creds already configured (aws configure; NOT the Bedrock token)
  python -m train.convert                       # produce train/dataset/sft.jsonl
  python -m train.launch_sm --model Qwen/Qwen2.5-3B-Instruct --instance ml.g5.xlarge --spot
  # -> downloads the trained LoRA adapter to train/checkpoints/student, then:
  python -m train.merge --adapter train/checkpoints/student

Set CODE_SM_ROLE (the SageMaker execution-role ARN; or edit the constant below) before the first
run. CODE_SM_BUCKET is optional — unset uses SageMaker's default bucket.
"""
import argparse
import os
import shutil
import tarfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ---- your AWS setup (edit, or set via env) ----
BUCKET = os.environ.get("CODE_SM_BUCKET", "")
ROLE = os.environ.get("CODE_SM_ROLE", "")
REGION = os.environ.get("AWS_REGION", "us-east-1")

DATA_LOCAL = os.path.join(ROOT, "train", "dataset", "sft.jsonl")
OUT_LOCAL = os.path.join(ROOT, "train", "checkpoints", "student")
STAGE_DIR = os.path.join(ROOT, ".sm_src")        # staged source_dir (gitignored)
DL_DIR = os.path.join(ROOT, ".sm_out")           # downloaded artifact scratch (gitignored)


def stage_source(data_path):
    """Build a clean SageMaker source dir: the SFT entry script, a TORCH-FREE requirements.txt
    (the container already ships torch), AND the dataset itself. BUNDLING the (tiny) dataset here
    means SageMaker's default bucket carries it with the code — no separate data upload, no data
    bucket to manage; it lands next to sft.py in /opt/ml/code, where sft.py finds it. SageMaker
    auto-installs source_dir/requirements.txt. Returns the dir."""
    if os.path.isdir(STAGE_DIR):
        shutil.rmtree(STAGE_DIR)
    os.makedirs(STAGE_DIR)
    shutil.copy(os.path.join(ROOT, "train", "sft.py"), os.path.join(STAGE_DIR, "sft.py"))
    shutil.copy(os.path.join(ROOT, "train", "requirements-sm.txt"),
                os.path.join(STAGE_DIR, "requirements.txt"))
    shutil.copy(data_path, os.path.join(STAGE_DIR, "sft.jsonl"))
    return STAGE_DIR


def main():
    ap = argparse.ArgumentParser(description="Launch openagent-code LoRA-SFT on SageMaker (spot-capable)")
    ap.add_argument("--model", default="Qwen/Qwen2.5-3B-Instruct",
                    help="HF id of the student base (a 3B fits a 24GB instance; 20B needs --load_4bit)")
    ap.add_argument("--data", default=DATA_LOCAL,
                    help="local sft.jsonl to bundle into the job (default: train/dataset/sft.jsonl)")
    ap.add_argument("--epochs", type=float, default=1.0)
    ap.add_argument("--lr", type=float, default=2e-4)
    ap.add_argument("--batch", type=int, default=1)
    ap.add_argument("--grad_accum", type=int, default=8)
    ap.add_argument("--max_len", type=int, default=4096)
    ap.add_argument("--load_4bit", action="store_true",
                    help="4-bit base (bitsandbytes) - needed to fit a 20B on a 24GB instance")
    ap.add_argument("--instance", default="ml.g5.xlarge",
                    help="ml.g5.xlarge (A10G 24GB) for a 3B LoRA; ml.g6e.xlarge (L40S 48GB) for 20B-4bit")
    ap.add_argument("--spot", action="store_true", help="managed spot training (~50-70%% off)")
    ap.add_argument("--max_hours", type=float, default=6.0, help="max_run; max_wait = 1.5x for spot")
    ap.add_argument("--no_download", action="store_true",
                    help="don't pull the adapter artifact after the job (just print its S3 URI)")
    args = ap.parse_args()

    if not ROLE:
        raise SystemExit("Set CODE_SM_ROLE (the SageMaker execution-role ARN) - env / .env, or the "
                         "constant at the top of train/launch_sm.py - before launching.")
    if not os.path.isfile(args.data):
        raise SystemExit(f"No dataset at {args.data} - run `python -m train.convert` first.")

    try:
        import sagemaker
        from sagemaker.pytorch import PyTorch
        from sagemaker.s3 import S3Downloader
    except ImportError:
        raise SystemExit("SageMaker SDK not installed. Run: pip install -e \".[sagemaker]\"")

    sess = sagemaker.Session()   # default bucket (auto-provisioned) carries source_dir + the output

    # SageMaker passes hyperparameters as `--key value`; keys match train/sft.py's argparse
    # (hyphens included). --load-4bit is a nargs="?" flag there, so a "true"/"false" value works.
    hp = {
        "model": args.model, "epochs": args.epochs, "lr": args.lr, "batch": args.batch,
        "grad-accum": args.grad_accum, "max-len": args.max_len,
        "load-4bit": "true" if args.load_4bit else "false",
    }
    # HF_TOKEN is forwarded ONLY if set (private base models); it stays in your shell, never stored.
    environment = {"HF_TOKEN": os.environ["HF_TOKEN"]} if os.environ.get("HF_TOKEN") else {}

    max_run = int(args.max_hours * 3600)
    est = PyTorch(
        entry_point="sft.py",
        source_dir=stage_source(args.data),
        role=ROLE,
        instance_type=args.instance,
        instance_count=1,
        framework_version="2.4",
        py_version="py311",
        hyperparameters=hp,
        environment=environment,
        use_spot_instances=bool(args.spot),
        max_run=max_run,
        max_wait=int(max_run * 1.5) if args.spot else None,
        base_job_name="openagent-sft",
        sagemaker_session=sess,
    )
    print(f"launching {args.instance} {'(spot)' if args.spot else '(on-demand)'} | model {args.model} "
          f"| dataset bundled ({os.path.getsize(args.data) // 1024} KB)")
    est.fit()   # dataset is bundled in source_dir; no data channel. blocks + streams logs.

    # The trained LoRA adapter is SageMaker's model artifact (a model.tar.gz in S3, built from
    # whatever train/sft.py wrote to SM_MODEL_DIR). Pull + unpack it into the local checkpoints dir
    # so `python -m train.merge` finds it exactly where it expects (no path juggling).
    if args.no_download:
        print(f"adapter artifact: {est.model_data}")
        return
    if os.path.isdir(DL_DIR):
        shutil.rmtree(DL_DIR)
    os.makedirs(DL_DIR)
    S3Downloader.download(est.model_data, DL_DIR, sagemaker_session=sess)
    os.makedirs(OUT_LOCAL, exist_ok=True)
    with tarfile.open(os.path.join(DL_DIR, "model.tar.gz")) as t:
        t.extractall(OUT_LOCAL)
    shutil.rmtree(DL_DIR, ignore_errors=True)
    rel = os.path.relpath(OUT_LOCAL, ROOT).replace(os.sep, "/")
    print(f"adapter -> {rel}   (next: python -m train.merge --adapter {rel})")


if __name__ == "__main__":
    main()
