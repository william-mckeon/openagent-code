"""
src/config.py

openagent-code — runtime configuration.

Configuration is read from CODE_* environment variables, each with a safe
default below, mirroring how openagent-infra and openagent-logger read their
config (os.environ.get with defaults). There is no YAML config file: .env is
the single source of local config, env_file delivers the same vars under
docker-compose, and the Dockerfile sets the in-image defaults.

`.env.example` documents every variable here. Load order:
    Dockerfile ENV defaults  <  .env (local) / env_file (compose)  <  real env
"""
import os
import json

from dotenv import load_dotenv

# Load .env for local (non-Docker) development. Under docker-compose the values
# arrive via env_file, so this is a harmless no-op there.
load_dotenv()


def _as_bool(value: str) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


# -----------------------------------------------------------------------------
# Model gateway (the swappable boundary)
#
# CODE_MODEL      LiteLLM model string. RunPod/vLLM: "openai/gpt-oss-120b".
#                 Bedrock: "bedrock/openai.gpt-oss-120b-1:0".
# CODE_API_BASE   OpenAI-compatible endpoint URL (self-hosted vLLM / RunPod).
#                 Leave empty for Bedrock (it uses AWS_* credentials instead).
# CODE_API_KEY    Bearer/key for the endpoint. vLLM's --api-key, or "EMPTY".
# CODE_TEMPERATURE  Sampling temperature for the agent loop.
# -----------------------------------------------------------------------------
MODEL = os.environ.get("CODE_MODEL", "openai/gpt-oss-120b")
API_BASE = os.environ.get("CODE_API_BASE", "")
API_KEY = os.environ.get("CODE_API_KEY", "")
TEMPERATURE = float(os.environ.get("CODE_TEMPERATURE", "0.2"))

# CODE_REASONING_EFFORT — gpt-oss reasoning depth: low | medium | high. Higher means
# the model deliberates more before answering — the lever against a weaker model
# answering instantly (confabulating a review) instead of investigating / calling tools.
# Sent to the endpoint as `reasoning_effort`. Empty/invalid = don't send it (use the
# endpoint's own default), which keeps behaviour unchanged unless you opt in.
_EFFORTS = {"low", "medium", "high"}
_effort = os.environ.get("CODE_REASONING_EFFORT", "").strip().lower()
REASONING_EFFORT = _effort if _effort in _EFFORTS else ""

# CODE_TOOL_MODE — how the model invokes tools:
#   "native" — OpenAI tool-calling. Default. Requires the serving stack to parse
#              tool calls; for gpt-oss on vLLM, launch the worker with
#              --enable-auto-tool-choice --tool-call-parser. Cleanest, and the
#              reliable path for agentic/investigative tasks.
#   "json"   — prompt-based fallback: tools are described in the system prompt and
#              the model replies with a JSON action we parse ourselves. Works on ANY
#              OpenAI-compatible endpoint with no server tool-parser, but is brittle
#              for gpt-oss on multi-step tasks — use only when native is unavailable.
TOOL_MODE = os.environ.get("CODE_TOOL_MODE", "native").strip().lower()

# CODE_MODEL_RETRIES — retry transient model failures (connection/timeout/5xx) AND
# dropped-tool-call responses (native mode: empty content + no tool_calls — the
# signature of a worker missing the tool-call parser). Lets the agent grind through
# a flaky / intermittent endpoint instead of failing the turn. 0 = no retries.
# Default 5 (not 3): serverless Bedrock throws bursts of transient 503s on large
# requests, and 3 short tries gave up before the burst cleared.
MODEL_RETRIES = int(os.environ.get("CODE_MODEL_RETRIES", "5"))

# CODE_BACKOFF_CAP — max seconds for one retry's exponential backoff (jitter added on
# top). Raised from the old hard-coded 8s so retries can outwait a Bedrock 503 burst.
BACKOFF_CAP = float(os.environ.get("CODE_BACKOFF_CAP", "20"))

# CODE_REQUEST_TIMEOUT — read timeout (seconds) for a SINGLE model call. Generous
# ON PURPOSE, copied from openagent-infra: a scale-to-zero serverless worker
# cold-starts on its first call after an idle period (tens of seconds), and a short
# timeout would ABORT that spin-up. openagent-infra absorbs the cold start at call
# time with a 600s read timeout rather than failing fast; we do the same.
REQUEST_TIMEOUT = float(os.environ.get("CODE_REQUEST_TIMEOUT", "600"))

# CODE_WARMUP / CODE_WARMUP_BUDGET — absorb a cold start ONCE, up front. Before the
# first task, warm_up() sends a throwaway tool-call probe and waits (up to
# WARMUP_BUDGET seconds) until a real tool_call comes back — i.e. the worker is warm
# AND its tool-call parser is active. This is the active form of infra's "absorb the
# cold start at call time": it stops the first real task from eating the cold start
# (and burning its CODE_MODEL_RETRIES on the empty responses a cold worker returns).
# No-op when CODE_API_BASE is empty (e.g. Bedrock). CODE_WARMUP=false skips it.
WARMUP = _as_bool(os.environ.get("CODE_WARMUP", "true"))
# 600s matches openagent-infra's read timeout: outwait the FULL serverless spin-up in
# ONE patient wait, rather than giving up at a short budget and then thrashing
# (give-up -> the real call drops -> re-warm -> repeat). The real cure for cold starts
# is a min-active worker on the RunPod endpoint (no scale-to-zero); this just makes the
# unavoidable first wait a single one. Set 0 / CODE_WARMUP=false to skip.
WARMUP_BUDGET = float(os.environ.get("CODE_WARMUP_BUDGET", "600"))

# -----------------------------------------------------------------------------
# Agent loop
#
# CODE_WORKSPACE      Directory the agent reads/edits. Defaults to the current
#                     working dir; the Docker image sets it to /workspace (the
#                     mounted repo).
# CODE_MAX_STEPS      Hard cap on model<->tool iterations per task.
# CODE_AUTO_APPROVE   Auto-approve write/edit/run (true), or confirm each (false).
# CODE_VERBOSE        Print tool activity to stdout.
# -----------------------------------------------------------------------------
WORKSPACE = os.environ.get("CODE_WORKSPACE") or os.getcwd()
MAX_STEPS = int(os.environ.get("CODE_MAX_STEPS", "25"))
AUTO_APPROVE = _as_bool(os.environ.get("CODE_AUTO_APPROVE", "true"))
VERBOSE = _as_bool(os.environ.get("CODE_VERBOSE", "true"))

# -----------------------------------------------------------------------------
# Permissions (Phase 4 #6) — the engine that gates every tool call. See
# specs/0001-permissions.md for the full contract (modes, rules, fence, precedence).
#
# CODE_PERMISSION_MODE  How much to auto-approve:
#   default     — mutating tools need approval (prompt if a human is present, else block)
#   acceptEdits — auto-approve write_file/edit_file; run_command still gated
#   plan        — read-only: every mutating tool is blocked
#   bypass      — auto-approve everything (today's CODE_AUTO_APPROVE=true behaviour)
#   Unset/invalid -> derived from CODE_AUTO_APPROVE (true=bypass, false=default) so
#   existing configs keep working unchanged.
# CODE_PERMISSIONS_CONFIG  Path to a JSON file of allow/ask/deny rules (see
#   permissions.json.example). Matchers are tool_name(pattern): run_command(rm:*),
#   edit_file(src/**), read_file(.env). deny always wins (even under bypass).
# CODE_ADD_DIRS  Extra directories the file tools may touch, beyond the workspace
#   (os.pathsep- or comma-separated). The workspace root is always allowed; this
#   widens the fence. Set to the filesystem root to effectively disable confinement.
# -----------------------------------------------------------------------------
_MODES = {"default", "acceptEdits", "plan", "bypass"}
PERMISSION_MODE = os.environ.get("CODE_PERMISSION_MODE", "").strip()
PERMISSIONS_CONFIG = os.environ.get("CODE_PERMISSIONS_CONFIG", "").strip()
ADD_DIRS = os.environ.get("CODE_ADD_DIRS", "").strip()


def resolved_permission_mode() -> str:
    """The effective mode: explicit CODE_PERMISSION_MODE, else derived from
    CODE_AUTO_APPROVE (back-compat). An invalid value falls back to the derived one."""
    if PERMISSION_MODE in _MODES:
        return PERMISSION_MODE
    return "bypass" if AUTO_APPROVE else "default"


def load_permission_rules() -> dict:
    """Read the allow/ask/deny rules from CODE_PERMISSIONS_CONFIG. Missing file or
    unset path -> empty rule set (mode alone governs). Never raises on a bad file."""
    empty = {"deny": [], "ask": [], "allow": []}
    if not PERMISSIONS_CONFIG or not os.path.isfile(PERMISSIONS_CONFIG):
        return empty
    try:
        with open(PERMISSIONS_CONFIG, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return empty
    return {k: list(data.get(k) or []) for k in empty}


def permission_extra_roots() -> list:
    """Absolute, realpath'd extra roots from CODE_ADD_DIRS (workspace is added at
    call time from the agent's cwd, so it isn't included here)."""
    parts = []
    for chunk in ADD_DIRS.replace(",", os.pathsep).split(os.pathsep):
        d = chunk.strip()
        if d:
            parts.append(os.path.realpath(d))
    return parts

# -----------------------------------------------------------------------------
# Context compaction (Phase 4)
#
# CODE_COMPACT_AT_TOKENS  Estimated-token budget for the LIVE context. When the
#                         working set exceeds it, older turns are summarized to
#                         fit. This shrinks only what the model SEES — the full
#                         raw history is still logged (see ROADMAP "capture vs
#                         context"). 0 disables compaction.
# CODE_COMPACT_KEEP_RECENT  How many of the most-recent working messages to keep
#                         verbatim (never summarized).
# -----------------------------------------------------------------------------
COMPACT_AT_TOKENS = int(os.environ.get("CODE_COMPACT_AT_TOKENS", "16000"))
COMPACT_KEEP_RECENT = int(os.environ.get("CODE_COMPACT_KEEP_RECENT", "8"))

# CODE_MAX_SUBAGENT_DEPTH — how deep spawn_agent can nest (Phase 4).
#   0 = subagents disabled, 1 = one level (top-level agent may spawn, children
#   may not), 2 = children may spawn too, etc. Enforced at the spawn_agent tool.
MAX_SUBAGENT_DEPTH = int(os.environ.get("CODE_MAX_SUBAGENT_DEPTH", "1"))

# -----------------------------------------------------------------------------
# External tools (Phase 4 tool breadth) — these reach OFF the machine, so they
# are OPT-IN to preserve the data-sovereignty default.
#
# CODE_ENABLE_WEB   Master switch for web_fetch / web_search. Default off — when
#                   off, the web tools aren't even offered to the model.
# CODE_SEARCH_URL   BYO search endpoint web_search POSTs {"query": ...} to. Unset
#                   = web_search reports "not configured".
# CODE_SEARCH_KEY   Optional bearer token for CODE_SEARCH_URL.
# -----------------------------------------------------------------------------
ENABLE_WEB = _as_bool(os.environ.get("CODE_ENABLE_WEB", "false"))
SEARCH_URL = os.environ.get("CODE_SEARCH_URL", "")
SEARCH_KEY = os.environ.get("CODE_SEARCH_KEY", "")

# CODE_MCP_CONFIG — path to a JSON file listing MCP servers to connect (stdio):
#   { "mcpServers": { "<name>": { "command": "...", "args": [...], "env": {...} } } }
# Unset = MCP off. Each server's tools appear as mcp__<name>__<tool>.
MCP_CONFIG = os.environ.get("CODE_MCP_CONFIG", "")

# -----------------------------------------------------------------------------
# Cross-session memory (Phase 4 #7) — see specs/0002-memory.md.
#
# CODE_MEMORY        Master switch. OFF by default (opt-in): memory writes a file
#                    into the target repo, and it must stay off for eval so the
#                    harness stays isolated/reproducible. On = the `remember` tool
#                    is offered and the memory file is loaded into the system prompt.
# CODE_MEMORY_FILE   Per-project memory file, resolved relative to the workspace.
# CODE_MEMORY_MAX_CHARS  Cap on how much memory is loaded into context (keeps the
#                    system prompt bounded; the most-recent content is kept).
# -----------------------------------------------------------------------------
MEMORY = _as_bool(os.environ.get("CODE_MEMORY", "false"))
MEMORY_FILE = os.environ.get("CODE_MEMORY_FILE", ".openagent/memory.md")
MEMORY_MAX_CHARS = int(os.environ.get("CODE_MEMORY_MAX_CHARS", "4000"))


def memory_file(workspace: str) -> str:
    """Absolute path to the project memory file (relative values resolve against ws)."""
    f = MEMORY_FILE
    return f if os.path.isabs(f) else os.path.join(workspace, f)

# -----------------------------------------------------------------------------
# Training flywheel
#
# CODE_TRAJECTORY_DIR  Where session JSONL is written. Relative paths resolve
#                      against CODE_WORKSPACE.
# CODE_VERIFY_COMMAND  Objective reward signal: the command that proves a change
#                      works in the target repo, e.g. "pytest -q". Empty = skip.
# -----------------------------------------------------------------------------
TRAJECTORY_DIR = os.environ.get("CODE_TRAJECTORY_DIR", "trajectories")
VERIFY_COMMAND = os.environ.get("CODE_VERIFY_COMMAND", "")

# CODE_SFT_VIEW — which captured view train/convert.py flattens into SFT rows:
#   "raw"     — the full raw history, every turn uncompacted (the source of truth). Default.
#   "as_sent" — what the model actually saw (post-compaction context); use to train
#               the model to work well FROM compacted context.
SFT_VIEW = os.environ.get("CODE_SFT_VIEW", "raw").strip().lower()


def trajectory_dir() -> str:
    """Absolute trajectory dir (relative values resolve against the workspace)."""
    d = TRAJECTORY_DIR
    return d if os.path.isabs(d) else os.path.join(WORKSPACE, d)


def display_model() -> str:
    """The model id as FORWARDED to the endpoint, for banners only.

    LiteLLM strips the leading provider segment to route, then sends the rest as
    the model id. So a deliberately double-prefixed `openai/openai/gpt-oss-120b`
    (provider `openai` + served id `openai/gpt-oss-120b`) shows as the served id
    rather than the raw routing string. Cosmetic only — MODEL stays the routing
    value used for calls and logged to the trajectory.
    """
    return MODEL.split("/", 1)[1] if "/" in MODEL else MODEL
