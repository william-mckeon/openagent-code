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

# -----------------------------------------------------------------------------
# Agent identity (Phase 36 / specs/0036) — the name the agent answers to, chosen at install.
#
# CODE_AGENT_NAME     The user-facing NAME (system-prompt identity line + the two cli.py banners). Default
#                     "OAC". The package / import name stays openagent-code; only the display identity moves.
#                     A blank value coalesces back to "OAC" so the prompt can never emit "You are ,".
# CODE_AGENT_PERSONA  An OPTIONAL short persona line appended to the system prompt, single-line + capped.
#                     Empty (default) appends nothing. Managed by `openagent-code --set-name/--remove-name`.
# -----------------------------------------------------------------------------
AGENT_NAME = (os.environ.get("CODE_AGENT_NAME", "OAC").strip() or "OAC")
PERSONA_MAX = 280
AGENT_PERSONA = os.environ.get("CODE_AGENT_PERSONA", "").replace("\n", " ").replace("\r", " ").strip()[:PERSONA_MAX]


def agent_name() -> str:
    """The display / launch name (specs/0036). The single normalization choke point; a blank name is the
    default "OAC" (never an empty identity)."""
    return (AGENT_NAME or "").strip() or "OAC"


def agent_persona() -> str:
    """The optional persona line, re-sanitized on READ (defense in depth): single-line, capped, may be ''."""
    return (AGENT_PERSONA or "").replace("\n", " ").replace("\r", " ").strip()[:PERSONA_MAX]

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


_MODES = {"default", "acceptEdits", "plan", "bypass", "propose"}
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
# Grounding path accuracy (Phase 27 / specs/0027). Two default-OFF improvements to how cited PATHS are
# checked: (1) the deterministic present-path existence check ALSO runs in semantic mode (the Tier-2
# verifier is fail-open and waves through a phantom PRESENT-path citation - a described file that doesn't
# exist, e.g. a Dockerfile the answer detailed but never wrote); (2) extension-less well-known filenames
# (Dockerfile / Makefile / ...) are recognized by the strict extractor so they get existence-checked; and
# the grounding VERIFIER subagent is told the granted reference dirs so it can read a cited granted-dir file
# by absolute path. OFF -> byte-identical (the existence check stays semantic-off-only, no extra names,
# the verifier isn't handed granted dirs).
VERIFY_GROUNDING_PATHS = _as_bool(os.environ.get("CODE_VERIFY_GROUNDING_PATHS", "false"))

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

# Working-directory prompt (Phase 30 / specs/0030). Pin the ABSOLUTE workspace path in the DURABLE system
# prompt (never compacted), and teach that a granted reference dir is a READ SOURCE while a copy/create
# DESTINATION is the workspace. Fixes the log-observed slip where, after compaction, a "copy the .env" write
# was proposed into the granted SOURCE tree instead of the working dir. OFF by default -> the workspace path
# isn't rendered and the granted-dirs note keeps its old text (byte-identical).
WORKDIR_PROMPT = _as_bool(os.environ.get("CODE_WORKDIR_PROMPT", "false"))

# Trusted user directories (Phase 35 / specs/0035). Treat a directory the USER LITERALLY TYPED in their REPL
# message as an explicit READ grant (src/userdirs.py extracts it; cli.py grants it into the new
# Permissions.read_only_roots tier that MUTATING tools ignore), and let request_dir AUTO-GRANT an existing
# dir under BYPASS at the top level with a human present (no [y/N]) instead of prompting. Fixes a live
# session where a project the user named twice was never reviewed (the model corrupted the typed path and
# request_dir dead-ended). READ-only by construction: read_only_roots widens reads, never writes. OFF by
# default -> read_only_roots stays empty, the extractor/auto-grant never run, request_dir is byte-identical.
TRUST_USER_DIRS = _as_bool(os.environ.get("CODE_TRUST_USER_DIRS", "false"))

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
# Mass-destruction backstop (ride-5): a HARD per-turn ceiling on DESTRUCTIVE ops (delete / move /
# dangerous command) the guardian may auto-approve headless. The guardian reviews one call at a time and
# is aggregate-blind, so a decomposed bulk delete would be rubber-stamped file-by-file; this deterministic
# cap denies the (N+1)-th regardless of the reviewer's verdict ("escalate to a human"). Raise it to allow
# a bigger unattended sweep (a deliberate, auditable act). 0 disables the cap.
try:
    GUARDIAN_MAX_DESTRUCTIVE = max(0, int(os.environ.get("CODE_GUARDIAN_MAX_DESTRUCTIVE", "5")))
except ValueError:
    GUARDIAN_MAX_DESTRUCTIVE = 5

# hooks (Phase 15 / specs/0015). Opt-in, FAIL-OPEN lifecycle scripts around every tool call: a PreToolUse
# hook can DENY any tool (a policy about the EFFECT, not the tool name - closes "deny is tool-scoped"); a
# PermissionRequest hook can approve/deny an ask-tier call (deterministic sibling of the guardian); a
# PostToolUse hook observes the result. A missing / crashing / slow / non-JSON hook is IGNORED (fail-open)
# - hooks add restrictions + observability, they NEVER weaken the deny rules + fence + sandbox. Off by
# default -> decide() / the tool loop never consult hooks and are byte-identical to today.
HOOKS = _as_bool(os.environ.get("CODE_HOOKS", "false"))
HOOKS_CONFIG = _resolve_install_path(os.environ.get("CODE_HOOKS_CONFIG", ""))

# goal loop (Phase 20 / specs/0020). The agent declares a MACHINE-CHECKABLE bar via the `pursue` tool and
# the HARNESS iterates until the bar passes: the model never decides "done", the bar command does. Unlike
# 0014's verifier (operator-configured argv), this bar is MODEL-proposed, so it is argv-only (shell=False),
# entry-filtered (no DANGEROUS / no shell interpreter), and permission-gated. Off by default -> `pursue`
# isn't even offered to the model and the gate is a no-op (byte-identical).
GOAL_LOOP = _as_bool(os.environ.get("CODE_GOAL_LOOP", "false"))
# Hard ceiling on bar iterations, whatever the model asks for. The destructive cap counts DISTINCT targets,
# so it does NOT bound a bar re-running — this is the ONLY thing that does.
try:
    GOAL_MAX_ITERATIONS = max(1, int(os.environ.get("CODE_GOAL_MAX_ITERATIONS", "3")))
except ValueError:
    GOAL_MAX_ITERATIONS = 3
# Steps kept in reserve: run() falls THROUGH the gate chain to the synthesis path when max_steps runs out
# (returning 'max_steps', not 'goal_unmet'), so the gate stops re-prompting this close to the ceiling.
try:
    GOAL_STEP_HEADROOM = max(1, int(os.environ.get("CODE_GOAL_STEP_HEADROOM", "6")))
except ValueError:
    GOAL_STEP_HEADROOM = 6
# Seconds a single bar run may take before it's killed (a hung bar must not hang the loop).
try:
    GOAL_TIMEOUT = max(1, int(os.environ.get("CODE_GOAL_TIMEOUT", "120")))
except ValueError:
    GOAL_TIMEOUT = 120
# OPTIONAL operator allowlist: a JSON file of permitted bar argv lists (e.g. [["npm","test"],["pytest"]]).
# Unset = no allowlist (the entry filter + permission gate still apply). Set = ONLY these bars may run.
GOAL_BARS_CONFIG = _resolve_install_path(os.environ.get("CODE_GOAL_BARS_CONFIG", ""))


def load_goal_bars() -> list:
    """The operator's allowlist of permitted bar argv lists from CODE_GOAL_BARS_CONFIG. Missing / unset /
    bad file -> [] (no allowlist). Never raises. argv lists ONLY — a shell string is never accepted."""
    if not GOAL_BARS_CONFIG or not os.path.isfile(GOAL_BARS_CONFIG):
        return []
    try:
        with open(GOAL_BARS_CONFIG, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    return [list(b) for b in data if isinstance(b, list) and b and all(isinstance(x, str) for x in b)]


# adaptive reasoning effort (Phase 21 / specs/0021). Match effort to task difficulty instead of a fixed
# level: the agent can self-escalate (the `escalate_effort` tool) and the harness auto-escalates when the
# run is STRUGGLING. The decision goes through a PLUGGABLE policy so an operator can swap the deterministic
# default for an opt-in online learner (or their own). Off by default -> effort is byte-identical to today.
ADAPTIVE_EFFORT = _as_bool(os.environ.get("CODE_ADAPTIVE_EFFORT", "false"))
# Which policy decides: 'off' (never escalate) | 'reactive' (deterministic default) | 'online' (the opt-in
# learner in src/effort_online.py) | a dotted 'module:Class' an operator wrote. Bad value -> reactive.
EFFORT_POLICY = os.environ.get("CODE_EFFORT_POLICY", "reactive").strip()
# The concrete floor to ladder from when the base effort is empty (send-nothing); and the hard ceiling.
_ef_floor = os.environ.get("CODE_EFFORT_FLOOR", "").strip().lower()
EFFORT_FLOOR = _ef_floor if _ef_floor in _EFFORTS else "medium"
_ef_max = os.environ.get("CODE_EFFORT_MAX", "").strip().lower()
EFFORT_MAX = _ef_max if _ef_max in _EFFORTS else "high"
# Struggle score at which the harness auto-escalates one rung (a single flaky retry shouldn't jump).
try:
    EFFORT_THRESHOLD = max(1, int(os.environ.get("CODE_EFFORT_THRESHOLD", "2")))
except ValueError:
    EFFORT_THRESHOLD = 2
# OPTIONAL state file for the online learner (its persisted per-signature stats). Unset = in-memory only.
EFFORT_STATE = _resolve_install_path(os.environ.get("CODE_EFFORT_STATE", ""))

# propose mode (Phase 22 / specs/0022). Before a substantive change the agent proposes a structured CHANGE
# MANIFEST (add/move/update/delete + why) and the user approves the whole plan ONCE, then it executes just
# that plan. `--mode propose` makes it mandatory; in the other modes the same tool is ELECTED for a broad/
# destructive change and asks a single [y/N]. This flag is the ONE master switch toolset.active_tools()
# reads to offer `propose_changes` and that decide()'s propose / off-plan branches are gated on. Selecting
# propose mode (CLI / env) turns it on in cli.main(), so the mode is never a dead read-only mode. Off by
# default -> the tool isn't offered and every new gate branch is skipped (byte-identical).
PROPOSE = _as_bool(os.environ.get("CODE_PROPOSE", "false"))

# Completion & manifest honesty (Phase 26 / specs/0026). Two independent, default-OFF nets that close
# seams where a "done" claim wasn't backed by a real change:
#   CODE_VERIFY_MANIFEST         - reconcile an APPROVED propose-mode manifest against the mutation ledger:
#                                  challenge an unapplied item, log an `applied` flag, and label a dropped
#                                  (empty) native finish `no_output` instead of a clean completion.
#   CODE_VERIFY_MUTATION_CLAIMS  - DEPRECATED (specs/0032): a deterministic grounding net that flags a
#                                  completed file-mutation claim ("I copied the folder") when the mutation
#                                  ledger is EMPTY. It is the ONE check anchored on free-text PROSE, not a
#                                  declared structured artifact (the brittle NL parsing specs/0007 rejected;
#                                  a live smoke test caught it false-flagging descriptive prose). Superseded
#                                  by the structured declared-done family (completion/manifest/acceptance/
#                                  goal); kept as an opt-in backstop. Prefer CODE_VERIFY_MANIFEST.
# Both OFF by default -> byte-identical: no reconciliation, no `applied` field, the dropped finish still
# returns `final`, and the grounding net never runs.
VERIFY_MANIFEST = _as_bool(os.environ.get("CODE_VERIFY_MANIFEST", "false"))
VERIFY_MUTATION_CLAIMS = _as_bool(os.environ.get("CODE_VERIFY_MUTATION_CLAIMS", "false"))


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


def load_hooks_config() -> dict:
    """Read PreToolUse / PostToolUse / PermissionRequest hook entries from CODE_HOOKS_CONFIG. Missing /
    unset / bad file -> no hooks. Never raises. Each entry: {"command": "<shell command>", "tools":
    ["write_file", ...] (optional filter), "timeout": <seconds> (optional)}."""
    empty = {"PreToolUse": [], "PostToolUse": [], "PermissionRequest": []}
    if not HOOKS_CONFIG or not os.path.isfile(HOOKS_CONFIG):
        return empty
    try:
        with open(HOOKS_CONFIG, encoding="utf-8") as f:
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

# CODE_MODEL_MAX_TOKENS — the model's HARD context window (Phase 34 / specs/0034). Unlike CODE_COMPACT_AT_TOKENS
# above (a SOFT compaction TRIGGER), this is the true ceiling the SENT context must never exceed. gpt-oss-120b
# is 131072. Env-first with this hardcoded default (NOT a litellm lookup at import — keeps startup fast and
# offline). Two internal budgets are DERIVED from it (not separate flags): the harness compacts UNDER
# COMPACT_HARD_AT_TOKENS (window minus output + estimate headroom), and a single summarize() call never renders
# more than SUMMARIZE_INPUT_MAX_TOKENS — so a resumed session's whole history is summarized in chunks instead of
# overflowing the window in one shot. Headroom matters: estimate_tokens undercounts, and the window must also
# hold the model's OUTPUT.
try:
    MODEL_MAX_TOKENS = max(8000, int(os.environ.get("CODE_MODEL_MAX_TOKENS", "131072")))
except ValueError:
    MODEL_MAX_TOKENS = 131072
COMPACT_HARD_AT_TOKENS = max(8000, MODEL_MAX_TOKENS - 12000)      # ~120k for a 131k window (output/estimate headroom)
SUMMARIZE_INPUT_MAX_TOKENS = max(8000, MODEL_MAX_TOKENS - 35000)  # ~96k (minus SUMMARIZE_PROMPT + output reserve)

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
# CODE_SEARCH_PROVIDER  Which web_search adapter to use (Phase 24 / specs/0024):
#                   generic (BYO CODE_SEARCH_URL, the DEFAULT — unchanged behavior) |
#                   tavily (hosted, LLM-optimized; CODE_SEARCH_KEY is the tvly-... key) |
#                   searxng (self-hosted, data-sovereign; CODE_SEARCH_URL) | brave
#                   (CODE_SEARCH_KEY) | a dotted "module:func" you wrote.
# CODE_SEARCH_URL   Endpoint for the generic / searxng providers. Unset = that provider
#                   reports "not configured" (tavily/brave don't use it).
# CODE_SEARCH_KEY   The provider credential: tavily API key / brave token, or an optional
#                   Bearer token for the generic endpoint.
# CODE_SEARCH_MAX_RESULTS  How many results web_search returns (default 5).
# -----------------------------------------------------------------------------
ENABLE_WEB = _as_bool(os.environ.get("CODE_ENABLE_WEB", "false"))
# 'generic' by default (NOT 'tavily'): defaulting to the flagship would silently break every existing
# CODE_SEARCH_URL-only setup the moment web is enabled (its URL ignored, "not configured" for a missing key).
SEARCH_PROVIDER = os.environ.get("CODE_SEARCH_PROVIDER", "generic").strip()
SEARCH_URL = os.environ.get("CODE_SEARCH_URL", "")
SEARCH_KEY = os.environ.get("CODE_SEARCH_KEY", "")
# Defensive int (config.py is imported on EVERY run, flag-off included): a bad value must not raise at import.
try:
    SEARCH_MAX_RESULTS = max(1, int(os.environ.get("CODE_SEARCH_MAX_RESULTS", "5")))
except ValueError:
    SEARCH_MAX_RESULTS = 5

# CODE_MCP_CONFIG — path to a JSON file listing MCP servers to connect (stdio):
#   { "mcpServers": { "<name>": { "command": "...", "args": [...], "env": {...}, "web": true } } }
# Unset = MCP off. Each server's tools appear as mcp__<name>__<tool>. A server marked "web": true (specs/0029,
# e.g. Tavily's MCP) has its tool output routed through the SAME untrusted-content fence + grounding
# read-ledger as web_fetch/web_search - so external web content it returns can't inject and a cited URL it
# surfaced grounds. A non-web server (git/filesystem) is unchanged.
MCP_CONFIG = os.environ.get("CODE_MCP_CONFIG", "")
# Runtime flag (NOT a CODE_* env var): mcp_client.connect() sets it True when a web-marked MCP server is
# connected, disconnect() resets it. web_grounding_active() lets the grounding gate treat MCP-surfaced web
# content exactly like native web content. Default False -> byte-identical when no web-marked MCP is present.
MCP_WEB_ACTIVE = False


def web_grounding_active():
    """True when web content can enter the model's context - native web tools (CODE_ENABLE_WEB) OR a
    web-marked MCP server (specs/0029). The grounding gate's web checks key on THIS so an MCP-surfaced URL is
    grounded / fed to the verifier exactly like a native one. False (both off) -> the web checks are skipped
    (byte-identical)."""
    return ENABLE_WEB or MCP_WEB_ACTIVE


def safety_fingerprint(perms=None):
    """A snapshot of the SAFETY + VERIFICATION config active when a run STARTED (Phase 33 / specs/0033),
    recorded in the trajectory's session_start so a clean guardian-ON run is never indistinguishable from a
    guardian-OFF one. A human-READABLE dict of flag -> value (not an opaque hash), so it directly answers
    "which guards were on". Reads EXISTING flags only - no new CODE_* var.

    `perms` (a Permissions) supplies the EFFECTIVE permission mode + fence width, which the config globals
    alone can't: Permissions.from_config resolves --mode / --add-dir ONTO the Permissions object and never
    writes back to config, so a `--mode acceptEdits` run leaves config.resolved_permission_mode() showing
    'bypass'. So permission_mode/extra_roots come from perms; everything else from module globals + the rule
    counts. Captured at session_start time -> it observes runtime-mutated flags (PROPOSE, MCP_WEB_ACTIVE) too.

    Snapshot scope (specs/0033 non-goal): this is the LAUNCH-time config. A --resume, or a mid-session REPL
    /mode or /add-dir, does NOT re-stamp it - a follow-up would add a resume/mode-change fingerprint."""
    return {
        # permission / fence gate - counts come from the EFFECTIVE Permissions object (perms.deny/ask/allow),
        # not a re-read of the file, so --mode / --add-dir and any programmatic rules are reflected.
        "permission_mode": getattr(perms, "mode", None) or resolved_permission_mode(),
        "permission_rules": {k: len(getattr(perms, k, None) or []) for k in ("deny", "ask", "allow")},
        "extra_roots": len(getattr(perms, "extra_roots", None) or []),
        "execpolicy": EXECPOLICY,
        "sandbox": SANDBOX,
        "guardian": GUARDIAN,
        "guardian_max_destructive": GUARDIAN_MAX_DESTRUCTIVE,
        "hooks": HOOKS,
        "hooks_config": bool(HOOKS_CONFIG and os.path.isfile(HOOKS_CONFIG)),
        # verification / done-honesty gates (the declared-done family, specs/0032)
        "verify_completion": VERIFY_COMPLETION,
        "verify_grounding": VERIFY_GROUNDING,
        "verify_grounding_semantic": VERIFY_GROUNDING_SEMANTIC,
        "verify_grounding_paths": VERIFY_GROUNDING_PATHS,
        "verify_touched": VERIFY_TOUCHED,
        "verify_manifest": VERIFY_MANIFEST,
        "verify_mutation_claims": VERIFY_MUTATION_CLAIMS,
        # plan / spec discipline
        "propose": PROPOSE,
        "spec_first": SPEC_FIRST,
        "goal_loop": GOAL_LOOP,
        # behavioral policy
        "adaptive_effort": ADAPTIVE_EFFORT,
        "effort_policy": EFFORT_POLICY,
        # external reach / untrusted-content boundary
        "enable_web": ENABLE_WEB,
        "mcp_web_active": MCP_WEB_ACTIVE,
        "web_grounding_active": web_grounding_active(),
        "workdir_prompt": WORKDIR_PROMPT,
    }

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
# Project todos (Phase 23 / specs/0023) — a persistent, per-project BACKLOG the agent maintains, the
# structured sibling of memory. Opt-in like memory (writes a checklist file INTO the target repo; off for
# eval so the harness stays isolated). On = the `project_todos` tool is offered and the backlog is loaded
# into the system prompt AND shown at the start of each session.
#
# CODE_PROJECT_TODOS            Master switch. OFF by default.
# CODE_PROJECT_TODOS_FILE       Per-project checklist file, resolved relative to the workspace.
# CODE_PROJECT_TODOS_MAX_CHARS  OUTER bound on how much of the backlog is injected into the prompt (capped
#                               by whole lines in src/todos.py, never a byte tail that would slice an item).
# -----------------------------------------------------------------------------
PROJECT_TODOS = _as_bool(os.environ.get("CODE_PROJECT_TODOS", "false"))
PROJECT_TODOS_FILE = os.environ.get("CODE_PROJECT_TODOS_FILE", ".openagent/todos.md")
PROJECT_TODOS_MAX_CHARS = int(os.environ.get("CODE_PROJECT_TODOS_MAX_CHARS", "4000"))


def todos_file(workspace: str) -> str:
    """Absolute path to the project todos file (relative values resolve against ws) — a clone of
    memory_file()."""
    f = PROJECT_TODOS_FILE
    return f if os.path.isabs(f) else os.path.join(workspace, f)

# -----------------------------------------------------------------------------
# Spec-first (Phase 25 / specs/0025) — the agent authors a persistent design+acceptance spec before a
# substantive change, gets it approved, and can't report done until the acceptance items are met. Opt-in
# like memory/todos (writes spec files INTO the repo; off for eval). On = the `write_spec` tool is offered,
# the ACTIVE spec is loaded into the system prompt + shown at startup, and the acceptance gate is armed.
#
# CODE_SPEC_FIRST            Master switch. OFF by default.
# CODE_SPECS_DIR             Per-project directory of NNNN-slug.md specs, resolved relative to the workspace
#                            (co-located with memory.md / todos.md), NOT the install root.
# CODE_SPECS_MAX_CHARS       Outer bound on how much of the active spec is injected into the prompt (capped
#                            by whole lines in src/specstore.py, never a byte tail that would slice an item).
# CODE_SPEC_FIRST_RETRIES    How many times the acceptance gate re-prompts before recording 'acceptance_unmet'.
# -----------------------------------------------------------------------------
SPEC_FIRST = _as_bool(os.environ.get("CODE_SPEC_FIRST", "false"))
SPECS_DIR = os.environ.get("CODE_SPECS_DIR", ".openagent/specs")
SPECS_MAX_CHARS = int(os.environ.get("CODE_SPECS_MAX_CHARS", "8000"))
try:
    SPEC_FIRST_RETRIES = max(1, int(os.environ.get("CODE_SPEC_FIRST_RETRIES", "2")))
except ValueError:
    SPEC_FIRST_RETRIES = 2


def specs_dir(workspace: str) -> str:
    """Absolute path to the project SPECS DIRECTORY (relative values resolve against ws, per-repo — like
    memory_file()/todos_file(), NOT install-root like skills_dir())."""
    f = SPECS_DIR
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
