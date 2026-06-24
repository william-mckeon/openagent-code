# =============================================================================
# openagent-code - GPU training image  (distillation Stage 5/6, specs/0005)
# =============================================================================
# A CUDA container for LoRA-SFT'ing the student with train/sft.py. Modeled on the
# proven boenet v2 CUDA pattern. The point: run the trainer in a clean LINUX env on
# a local NVIDIA GPU, which sidesteps the two blockers we hit on the Windows host —
# the Application-Control DLL block on numpy, and pip's torch/peft version roulette.
#
# Build:    docker compose build train
# Smoke:    docker compose run --rm train-smoke      # tiny model, CPU, proves the loop
# Train:    docker compose run --rm train python -m train.sft \
#               --model openai/gpt-oss-20b --epochs 1 --load-4bit
# Detached: docker compose run -d --name oac_train train python -m train.sft ... ; docker logs -f oac_train
#
# Prereqs: an NVIDIA GPU + driver, Docker + NVIDIA Container Toolkit.
#   Windows: Docker Desktop + WSL2 + an NVIDIA GPU (then `--gpus`/compose device works).
#
# The trainer code (train/), the dataset, checkpoints, and trajectories are MOUNTED at
# runtime (see docker-compose.yml), so editing a task or sft.py needs NO rebuild. Only
# src/ + the [train] dep set are baked into the image.
# =============================================================================

ARG CUDA_VERSION=12.8.0
ARG UBUNTU_VERSION=22.04
ARG PYTHON_VERSION=3.11
# PyTorch cu128 — includes Blackwell / RTX 50-series (sm_120) support. Drop to a
# cu124/cu121 wheel + an older torch if your card is Ampere/Ada and you prefer.
ARG PYTORCH_VERSION=2.7.1

# ----------------------------------------------------------------------------
FROM nvidia/cuda:${CUDA_VERSION}-cudnn-runtime-ubuntu${UBUNTU_VERSION} AS base

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=UTF-8 \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8

# ----------------------------------------------------------------------------
# System dependencies (+ a real Python on the CUDA base image)
# ----------------------------------------------------------------------------
FROM base AS system-deps
ARG PYTHON_VERSION
RUN apt-get update && apt-get install -y --no-install-recommends \
    python${PYTHON_VERSION} python${PYTHON_VERSION}-dev python${PYTHON_VERSION}-venv \
    python3-pip build-essential gcc g++ git curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* && apt-get clean
RUN update-alternatives --install /usr/bin/python python /usr/bin/python${PYTHON_VERSION} 1 \
    && update-alternatives --install /usr/bin/python3 python3 /usr/bin/python${PYTHON_VERSION} 1

# ----------------------------------------------------------------------------
# Python dependencies
# ----------------------------------------------------------------------------
FROM system-deps AS python-deps
RUN pip install --no-cache-dir --upgrade pip setuptools wheel

# Pinned CUDA torch FIRST, from the cu128 index. Installing it before the [train]
# extra means the extra's `torch>=2.1` is already satisfied, so pip won't yank a
# CPU build over the CUDA one (the exact failure mode we hit on the host).
ARG PYTORCH_VERSION
RUN pip install --no-cache-dir torch==${PYTORCH_VERSION} \
    --index-url https://download.pytorch.org/whl/cu128
RUN python -c "import torch; print('torch', torch.__version__, '| cuda', torch.version.cuda)"

WORKDIR /app
# Install the [train] extra via pyproject. Only src/ is needed at BUILD time (it's the
# package setuptools builds); train/ + data are mounted at runtime.
COPY pyproject.toml README.md ./
COPY src ./src
COPY eval ./eval
RUN pip install --no-cache-dir -e ".[train]"
RUN python -c "import transformers, peft, datasets, accelerate; print('train stack OK')"

# ----------------------------------------------------------------------------
# Application
# ----------------------------------------------------------------------------
FROM python-deps AS app
# Non-root user. Create the runtime mount points AS ROOT first, then hand ownership over.
RUN useradd --create-home --shell /bin/bash trainer \
    && mkdir -p /app/train/dataset /app/train/checkpoints /app/trajectories /app/.cache \
    && chown -R trainer:trainer /app
USER trainer

# HuggingFace / model cache inside /app/.cache — mount it (compose) so weight downloads
# persist across runs instead of re-downloading every time.
ENV HF_HOME=/app/.cache \
    HF_DATASETS_CACHE=/app/.cache \
    NVIDIA_VISIBLE_DEVICES=all \
    NVIDIA_DRIVER_CAPABILITIES=compute,utility \
    PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True

HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD python -c "import torch, transformers, peft; print('OK')" || exit 1

CMD ["python", "-m", "train.sft", "--help"]

LABEL org.opencontainers.image.title="openagent-code trainer" \
      org.opencontainers.image.description="CUDA LoRA-SFT trainer for distilling the student (specs/0005 Stage 5/6)" \
      cuda.version="12.8" \
      pytorch.cuda.index="cu128" \
      supported.architectures="Ampere, Ada Lovelace, Hopper, Blackwell (sm_120)"
