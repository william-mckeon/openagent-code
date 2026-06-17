# syntax=docker/dockerfile:1.7

# ============================================================================
# openagent-code - Dockerfile
# ============================================================================
#
# Container image for the openagent-code CLI coding agent.
#
#   - Base:        python:3.12-slim
#   - User:        openagent (uid 1000) — matches the OpenAgent services so file
#                  permissions on the mounted workspace line up
#   - Workspace:   /workspace — the target repo, mounted in at run time. The
#                  agent reads and edits files HERE; the image holds only the
#                  tool itself (under /app).
#
# Unlike the other OpenAgent services (openagent-api/-infra/-logger/-memory),
# this image is NOT a long-running uvicorn HTTP service. It is a one-shot CLI:
# it runs a single coding task, writes a trajectory, and exits. Therefore it
# has NO EXPOSE, NO HEALTHCHECK, and NO port — there is nothing to probe. It
# also does not join openagent-network; it depends on no other service and
# reaches its model endpoint directly via CODE_API_BASE.
#
# Build context is the project root, NOT this directory. The COPY
# instructions below resolve paths from the project root.
#
# Build:
#     docker build -t openagent-code:latest -f docker/code/Dockerfile .
#
# Run (mount the repo to work on as /workspace):
#     docker run --rm --env-file .env -v /path/to/repo:/workspace \
#         openagent-code:latest "add a docstring to foo.py and run the tests"
#
# The companion docker-compose.yml references this file via
# `build.dockerfile: docker/code/Dockerfile`.
# ============================================================================


FROM python:3.12-slim


# ----------------------------------------------------------------------------
# Image metadata (OCI standard labels)
# ----------------------------------------------------------------------------

LABEL org.opencontainers.image.title="openagent-code" \
      org.opencontainers.image.description="Self-hosted coding agent instrumented for training data" \
      org.opencontainers.image.vendor="William McKeon" \
      org.opencontainers.image.licenses="Apache-2.0"


# ----------------------------------------------------------------------------
# Environment
# ----------------------------------------------------------------------------
#
# Only the STABLE, build-affecting vars live here (they influence the pip layer or
# rarely change). The volatile CODE_* defaults are set lower, just above the source
# COPY, so tweaking one doesn't invalidate the apt/pip cache and force a full rebuild.
#
# PYTHONDONTWRITEBYTECODE / PYTHONUNBUFFERED  - standard Python-in-container
# PIP_NO_CACHE_DIR / PIP_DISABLE_PIP_VERSION_CHECK  - smaller, quieter pip
# PYTHONPATH=/app  - the `src` package lives at /app/src; WORKDIR is /workspace
#                    (the mounted repo), so `python -m src` needs /app on the path

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app


# ----------------------------------------------------------------------------
# OS dependencies
# ----------------------------------------------------------------------------
#
# - git: coding tasks frequently need it (status, diff, commit) via run_command
# - ca-certificates: lets Python verify TLS for the outbound model endpoint
#
# Everything else stays out so the image is small.

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git \
        ca-certificates \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*


# ----------------------------------------------------------------------------
# Non-root user
# ----------------------------------------------------------------------------
#
# UID 1000 is the conventional first non-root uid; keeping it consistent with
# the other OpenAgent services means file permissions on a shared/mounted
# volume line up.

RUN groupadd --system --gid 1000 openagent \
    && useradd --system \
        --uid 1000 \
        --gid openagent \
        --shell /usr/sbin/nologin \
        --create-home \
        openagent


# ----------------------------------------------------------------------------
# Application
# ----------------------------------------------------------------------------

WORKDIR /app

# Install Python dependencies FIRST (separate layer) so source-code changes
# do not invalidate the pip-install cache.
COPY requirements.txt /app/requirements.txt
RUN pip install --upgrade pip \
    && pip install --no-cache-dir -r /app/requirements.txt

# In-image CODE_* defaults — placed HERE (below the apt/pip layers) on purpose:
# they are volatile, so keeping them out of the early ENV means editing one only
# rebusts from this point down (cheap COPYs), not the expensive apt/pip layers.
# Override any of them via .env / -e at run time; they mirror the os.environ.get()
# defaults in src/config.py.
ENV CODE_WORKSPACE=/workspace \
    CODE_MODEL=openai/gpt-oss-120b \
    CODE_TOOL_MODE=native \
    CODE_MODEL_RETRIES=3 \
    CODE_REQUEST_TIMEOUT=600 \
    CODE_WARMUP=true \
    CODE_WARMUP_BUDGET=120 \
    CODE_MAX_STEPS=25 \
    CODE_COMPACT_AT_TOKENS=12000 \
    CODE_COMPACT_KEEP_RECENT=6 \
    CODE_MAX_SUBAGENT_DEPTH=1 \
    CODE_ENABLE_WEB=false

# Copy source. eval/ ships too so the harness (python -m eval.harness) can run
# IN-CONTAINER, not just locally — it imports from src/ and reads eval/tasks/*.yaml.
COPY src/ /app/src/
COPY eval/ /app/eval/

# Ownership: everything readable by the runtime user.
RUN chown -R openagent:openagent /app


# ----------------------------------------------------------------------------
# Runtime
# ----------------------------------------------------------------------------

USER openagent

# The agent operates on the mounted repo at /workspace, while its own code
# stays importable from /app via PYTHONPATH.
WORKDIR /workspace

# ENTRYPOINT is the CLI; arguments after the image name become the task prompt:
#     docker run ... openagent-code:latest "fix the failing test in foo.py"
ENTRYPOINT ["python", "-m", "src"]
