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

# The INSTALL ROOT — the openagent-code project dir (this file is <root>/src/config.py).
# Everything self-locates against this so the agent runs from ANY directory: the config,
# and the CENTRALIZED trajectory/log dirs, don't depend on the current working directory.
# That centralization is what makes the flywheel work across projects — every run, on any
# repo, writes its trajectory to ONE corpus (<root>/trajectories) that convert.py reads.
INSTALL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Load .env so config is found no matter where you launch from. A .env in the CURRENT dir
# wins (per-project override); the install-root .env fills the rest (your model/token).
# Real environment variables still take precedence over both. Under docker-compose the
# values arrive via env_file, so these file loads are harmless no-ops there.
load_dotenv()
load_dotenv(os.path.join(INSTALL_ROOT, ".env"), override=False)


def _as_bool(value: str) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


# -----------------------------------------------------------------------------
# Search / review traversal — ONE source of truth for the directories the search
# tools (grep / glob / tree) and the review orchestrator skip. These sets drifted
# before (tools.py listed 5 names, orchestrator.py 13), which let a dependency cache
# leak into the project map and become its own review area. They are VCS, caches,
# virtualenvs, build output, and dependency stores — third-party or generated, never
# the project's own code.
# -----------------------------------------------------------------------------
SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "node_modules", ".venv", "venv", "env", "vendor",
    "dist", "build", "target", ".next", ".nuxt", ".svelte-kit",
    ".idea", ".vscode", ".gradle", ".terraform", "trajectories",
})


def skip_walk_dir(name: str, parent_path: str) -> bool:
    """True if a directory met WHILE WALKING should be skipped: a noise/cache name above,
    or the Go module cache — a 'mod' directory directly under 'pkg' (i.e. .../pkg/mod),
    which is downloaded third-party code, not the project's own."""
    if name in SKIP_DIRS:
        return True
    return name == "mod" and os.path.basename(parent_path.rstrip("/\\")) == "pkg"


def skip_rel_path(rel: str) -> bool:
    """True if a RELATIVE path lies inside a skipped dir — for tools that match on the
    whole path (glob) rather than walking it directory by directory."""
    parts = rel.replace("\\", "/").strip("/").split("/")
    if any(p in SKIP_DIRS for p in parts):
        return True
    return any(parts[i] == "pkg" and parts[i + 1] == "mod" for i in range(len(parts) - 1))


def looks_like_dep_cache(path: str) -> bool:
    """True if `path` is a vendored dependency STORE rather than project code — e.g. a Go
    module cache (it contains mod/cache). Such a directory must never become a review area:
    reviewing it audits third-party downloads and starves the real source of attention."""
    return os.path.isdir(os.path.join(path, "mod", "cache"))


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
# 50 (raised from 25): a model<->tool round-trip is a STEP, not a token — a broad
# "review the whole repo" reads ~one file per step, so a low cap stops it before it
# can synthesize. The 128k window has the token headroom; let the loop take more
# round-trips. Pair with the on-max_steps synthesis turn (agent.py) so a capped run
# still returns an answer, and with subagent decomposition so big tasks fan out.
MAX_STEPS = int(os.environ.get("CODE_MAX_STEPS", "50"))
AUTO_APPROVE = _as_bool(os.environ.get("CODE_AUTO_APPROVE", "true"))
VERBOSE = _as_bool(os.environ.get("CODE_VERBOSE", "true"))

# CODE_LOG_DIR / CODE_LOG_LEVEL — the readable, portable SESSION LOG (src/logsetup.py): one
# .log file per run capturing tool calls + results, retries, errors, outcomes — built to be
# handed off for review (paste it to Claude to debug a run on any repo). Separate from the
# trajectory (training data) and the live console UX. CODE_LOG_DIR="" disables it.
LOG_DIR = os.environ.get("CODE_LOG_DIR", "logs")
LOG_LEVEL = os.environ.get("CODE_LOG_LEVEL", "INFO").strip().upper()

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
def _resolve_install_path(raw: str) -> str:
    """Resolve a config-file path so it's found no matter WHERE the agent runs from. Absolute
    paths are used as-is; a bare/relative path resolves against INSTALL_ROOT (where .env and
    permissions.json live), NOT the current workspace. Without this, running `oac` from another
    repo left a relative CODE_PERMISSIONS_CONFIG unresolvable, load_permission_rules() returned
    EMPTY, and every deny rule (read_file(.env), rm, curl, ...) silently evaporated under bypass."""
    raw = (raw or "").strip()
    if not raw or os.path.isabs(raw):
        return raw
    at_root = os.path.join(INSTALL_ROOT, raw)
    return at_root if os.path.isfile(at_root) else raw


_MODES = {"default", "acceptEdits", "plan", "bypass"}
PERMISSION_MODE = os.environ.get("CODE_PERMISSION_MODE", "").strip()
PERMISSIONS_CONFIG = _resolve_install_path(os.environ.get("CODE_PERMISSIONS_CONFIG", ""))
ADD_DIRS = os.environ.get("CODE_ADD_DIRS", "").strip()

# Verified completion (Phase 6 / specs/0007). When on, the agent may not report a task DONE
# while it has update_plan steps marked completed whose named file shows no real change — the
# harness challenges the mismatch (up to N times), then records an honest 'unverified_completion'.
VERIFY_COMPLETION = _as_bool(os.environ.get("CODE_VERIFY_COMPLETION", "true"))
VERIFY_COMPLETION_RETRIES = int(os.environ.get("CODE_VERIFY_COMPLETION_RETRIES", "2"))

# Grounding check (Phase 10 / specs/0010). After verified completion accepts a "done", the harness
# checks that the CLAIMS in the closing answer are grounded in the sources the agent cited/touched.
# Tier 1 (deterministic) flags a cited path that doesn't exist. Tier 2 (semantic, CODE_VERIFY_
# GROUNDING_SEMANTIC) spawns a CAPTURED verifier subagent that re-reads the sources and flags factual
# claims they don't support — the honest-but-wrong class (a real path, but the wrong one). A grounding
# failure is re-prompted (up to N), then recorded as an honest 'ungrounded_completion'. Tier 2 is on
# by default: it is the more agentic check AND each verifier is a captured trajectory for the flywheel.
VERIFY_GROUNDING = _as_bool(os.environ.get("CODE_VERIFY_GROUNDING", "true"))
VERIFY_GROUNDING_RETRIES = int(os.environ.get("CODE_VERIFY_GROUNDING_RETRIES", "2"))
VERIFY_GROUNDING_SEMANTIC = _as_bool(os.environ.get("CODE_VERIFY_GROUNDING_SEMANTIC", "true"))
# The reasoning effort the runtime Tier-2 grounding VERIFIER subagent runs at, INDEPENDENT of the
# coding agent's global CODE_REASONING_EFFORT — so the judge can run cheap/low while the agent stays
# high (calibrate with scripts/calibrate_grounding.py). Empty = inherit the global; else low|medium|high.
_g_effort = os.environ.get("CODE_GROUNDING_EFFORT", "").strip().lower()
GROUNDING_EFFORT = _g_effort if _g_effort in _EFFORTS else ""

# Corpus curation (Phase 11 / specs/0011). An OFFLINE batch pass (train/curate.py) over captured
# trajectories that flags PHANTOM CITATIONS — a closing answer referencing a file the run never opened.
# Deterministic, no model (the semantic honest-but-wrong class is caught live by the grounding gate).
# CODE_CURATE_MODE: 'flag' (default — tag rows, never shrink the tiny corpus) or 'exclude' (drop
# ungrounded sessions from the SFT set, counted in report.json's dropped ledger — no silent drops).
CURATE = _as_bool(os.environ.get("CODE_CURATE", "false"))
CURATE_MODE = os.environ.get("CODE_CURATE_MODE", "flag").strip().lower()
if CURATE_MODE not in ("flag", "exclude"):
    CURATE_MODE = "flag"

# Situational-context injection (Phase 12 / specs/0012). When on, a per-turn block of the agent's real
# environment (cwd, OS, shell, date, granted dirs) is injected as a refreshed, compaction-safe pin so the
# model conditions on live state instead of confabulating it. SITUATIONAL_GIT additionally appends a
# bounded git branch/status line (one git call per turn, not per step); only consulted when
# SITUATIONAL_CONTEXT is on. Both default OFF (opt-in, near-zero risk).
SITUATIONAL_CONTEXT = _as_bool(os.environ.get("CODE_SITUATIONAL_CONTEXT", "false"))
SITUATIONAL_GIT = _as_bool(os.environ.get("CODE_SITUATIONAL_GIT", "false"))

# Edit-layer (Phase 13 / specs/0013). CODE_EDIT_FUZZY: a SAFE fuzzy fallback UNDER exact-match edit_file
# - when the exact old_string isn't found, editmatch.resolve locates it (whitespace-insensitive, then
# most-similar chunk) and applies ONLY a UNIQUE, above-threshold match; any ambiguity refuses, so
# exact-match-first's never-silently-corrupt guarantee holds. Off by default. THRESHOLD is the similarity
# floor for the fuzziest tier - keep it conservative (a loose value is what reintroduces the risk).
EDIT_FUZZY = _as_bool(os.environ.get("CODE_EDIT_FUZZY", "false"))
try:
    EDIT_FUZZY_THRESHOLD = float(os.environ.get("CODE_EDIT_FUZZY_THRESHOLD", "0.9"))
except ValueError:
    EDIT_FUZZY_THRESHOLD = 0.9

# CODE_APPLY_PATCH (Phase 13 / specs/0013): offer the apply_patch tool - ONE envelope makes several file
# ops (Add/Update/Delete/Move) ATOMICALLY (all-or-nothing). Off by default; gated into the toolset by
# src/toolset.py. Every touched path is recorded on the mutation ledger, so the completion + grounding
# gates cover it with no other change.
APPLY_PATCH = _as_bool(os.environ.get("CODE_APPLY_PATCH", "false"))

# Auto-verify (Phase 14 / specs/0014). After the completion gate accepts a "done", run a configured check
# (default `python -m py_compile`) on just the TOUCHED files; on failure re-prompt to fix (bounded) then
# record an honest 'verify_failed_edits'. The command is OPERATOR-configured (never model-controlled) and
# runs as an ARGV list with no shell (no injection). CODE_VERIFY_CMDS_CONFIG is a JSON map of ext -> argv
# list, resolved against INSTALL_ROOT. All default OFF except the reward label. See specs/0014.
VERIFY_TOUCHED = _as_bool(os.environ.get("CODE_VERIFY_TOUCHED", "false"))
VERIFY_TOUCHED_RETRIES = int(os.environ.get("CODE_VERIFY_TOUCHED_RETRIES", "2"))
VERIFY_CMDS_CONFIG = _resolve_install_path(os.environ.get("CODE_VERIFY_CMDS_CONFIG", ""))
VERIFY_TOUCHED_LABEL = _as_bool(os.environ.get("CODE_VERIFY_TOUCHED_LABEL", "true"))
VERIFY_TIMEOUT = int(os.environ.get("CODE_VERIFY_TIMEOUT", "60"))

# execpolicy (Phase 16 / specs/0016). Gate run_command on its PARSED segments (read-only / mutating /
# dangerous) instead of a raw prefix: a deny/ask/allow rule then matches ANY segment (the `rm` inside
# `cd x && rm y`), and a wholly read-only command (ls, git status) is allowed like a read tool. Off by
# default -> permissions.decide() never consults execpolicy and the prefix matcher path is unchanged.
EXECPOLICY = _as_bool(os.environ.get("CODE_EXECPOLICY", "false"))

# sandbox — FS confinement (Phase 17 / specs/0017). Extend the workspace fence to run_command's WRITES:
# a command whose output redirect or write-command destination resolves OUTSIDE cwd + CODE_ADD_DIRS is
# REFUSED, so it can't write past the fence even under an allow rule / bypass. Off by default ->
# run_command never consults the sandbox and is byte-identical to today.
SANDBOX = _as_bool(os.environ.get("CODE_SANDBOX", "false"))

# guardian (Phase 19 / specs/0019). A fail-CLOSED LLM approval reviewer for the ASK tier: when a tool
# hits `ask`, a CAPTURED reviewer subagent decides approve/deny instead of the human prompt (so an
# unattended run can proceed on a REVIEWED ask-tier action instead of just blocking). Fail-closed - any
# error / ambiguous verdict DENIES. Off by default -> the human-prompt / headless-block path is unchanged.
GUARDIAN = _as_bool(os.environ.get("CODE_GUARDIAN", "false"))
_gd_effort = os.environ.get("CODE_GUARDIAN_EFFORT", "").strip().lower()
GUARDIAN_EFFORT = _gd_effort if _gd_effort in _EFFORTS else ""


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

# CODE_MAX_MESSAGE_CHARS — cap on a SINGLE message's content in the LIVE context (the full
# text is still logged raw to the trajectory). Stops one giant tool result (a huge file read,
# a long subagent return) from dominating the window and defeating compaction — which keeps
# recent messages verbatim and so can't shrink a huge recent one. ~48k chars ≈ 12k tokens.
# 0 disables the cap.
MAX_MESSAGE_CHARS = int(os.environ.get("CODE_MAX_MESSAGE_CHARS", "48000"))

# CODE_MAX_SUBAGENT_DEPTH — how deep spawn_agent can nest (Phase 4).
#   0 = subagents disabled, 1 = one level (top-level agent may spawn, children
#   may not), 2 = children may spawn too, etc. Enforced at the spawn_agent tool.
#   2 (raised from 1): lets a per-folder child decompose a large folder one level
#   further, so a whole-project review maps cleanly as a tree of summaries.
MAX_SUBAGENT_DEPTH = int(os.environ.get("CODE_MAX_SUBAGENT_DEPTH", "2"))

# CODE_MAX_SUBAGENT_FANOUT — how many children ONE agent may spawn (breadth). Depth
# alone doesn't bound cost: an agent told to "decompose" could otherwise spawn an
# unbounded number of subagents. Caps the fan-out per agent. Enforced at spawn_agent.
MAX_SUBAGENT_FANOUT = int(os.environ.get("CODE_MAX_SUBAGENT_FANOUT", "8"))

# CODE_MAX_REVIEW_AREAS — fan-out cap for the review_repo orchestrator specifically. Higher
# than MAX_SUBAGENT_FANOUT (which guards ad-hoc spawning) because a whole-repo review wants
# to cover EVERY top-level area, and each child is bounded and returns only a short summary.
MAX_REVIEW_AREAS = int(os.environ.get("CODE_MAX_REVIEW_AREAS", "16"))

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
# CODE_TRAJECTORY_DIR  Where session JSONL is written. Relative paths resolve against the
#                      INSTALL ROOT (not the workspace), so trajectories from every project
#                      you run on land in ONE corpus that the flywheel trains on.
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
    """Absolute trajectory dir. Relative values resolve against the INSTALL ROOT (not the
    workspace), so trajectories from EVERY repo you run the agent on land in ONE place —
    the flywheel's corpus, which convert.py reads (<install>/trajectories/**)."""
    d = TRAJECTORY_DIR
    return d if os.path.isabs(d) else os.path.join(INSTALL_ROOT, d)


# -----------------------------------------------------------------------------
# Skills (specs/0008) — reusable, harness-orchestrated workflows (SKILL.md files).
# Opt-in like memory; the skills dir SELF-LOCATES against the install root (like trajectories),
# so `oac` finds the skills corpus no matter which repo it runs in.
# -----------------------------------------------------------------------------
SKILLS = _as_bool(os.environ.get("CODE_SKILLS", "false"))
SKILLS_DIR = os.environ.get("CODE_SKILLS_DIR", "skills")


def skills_dir() -> str:
    """Absolute skills dir — a clone of trajectory_dir(): relative values resolve against the
    INSTALL ROOT, so the skills library is found regardless of the current workspace."""
    d = SKILLS_DIR
    return d if os.path.isabs(d) else os.path.join(INSTALL_ROOT, d)


def display_model() -> str:
    """The model id as FORWARDED to the endpoint, for banners only.

    LiteLLM strips the leading provider segment to route, then sends the rest as
    the model id. So a deliberately double-prefixed `openai/openai/gpt-oss-120b`
    (provider `openai` + served id `openai/gpt-oss-120b`) shows as the served id
    rather than the raw routing string. Cosmetic only — MODEL stays the routing
    value used for calls and logged to the trajectory.
    """
    return MODEL.split("/", 1)[1] if "/" in MODEL else MODEL
