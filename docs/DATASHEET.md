# openagent-code — DATASHEET

> Contract reference. The README is for newcomers; this file is for engineers
> integrating with openagent-code, operating it, or consuming the training data
> it produces.

| Field | Value |
|---|---|
| **Service name** | `openagent-code` |
| **Version** | 0.1.0 |
| **Role** | Self-hosted coding agent instrumented for training data |
| **Kind** | One-shot CLI (not an HTTP service; not on openagent-network) |
| **Base image** | `python:3.12-slim` |
| **Runtime user** | `openagent` (uid 1000) |
| **Workspace** | `/workspace` (the target repo, mounted at run time) |
| **Connectivity** | Outbound only: one OpenAI-compatible model endpoint (`CODE_API_BASE`) |
| **Status** | working — pre-production |

---

## 1. Ownership boundaries

### What this tool owns

| Domain | Concrete artifact |
|---|---|
| The agent loop | `src/agent.py` |
| The tool boundary | `src/tools.py` (read/grep/glob/write/edit/run_command) |
| The model gateway | `src/model.py` (LiteLLM → BYOC endpoint) |
| Behavioral scaffolding | `src/prompts.py` |
| Permission gating | `src/permissions.py` |
| Training-data capture | `src/trajectory.py`, the `trajectories/*.jsonl` files |
| The eval harness | `eval/harness.py`, `eval/tasks/*.yaml` |
| Configuration surface | `src/config.py` (the `CODE_*` variables) |

### What this tool does NOT own

| Concern | Owner | Notes |
|---|---|---|
| Model serving | Your provider | vLLM on RunPod / Bedrock; reached via `CODE_API_BASE` |
| Model weights | Your provider | Nothing is loaded or stored locally |
| Training / fine-tuning | `train/` (out of scope at runtime) | The agent only *captures*; training is a separate offline step |
| Secret management | The operator | `.env`, never committed |
| The repo being edited | The user | Mounted in as `/workspace`; the agent only reads/writes within it |
| Multi-service orchestration | n/a | openagent-code depends on no other OpenAgent service |

This list is the canonical answer to "should openagent-code do X?" — if X is in
the right-hand column, no.

---

## 2. Invocation contract

openagent-code is a CLI, not an HTTP service. Its "request" is a command line;
its "response" is the process exit code plus a trajectory file.

### 2.1 Command

```
python -m src "<task prompt>"     # one-shot autonomous run (deterministic; ask_user degrades)
python -m src                     # interactive REPL: multi-turn chat, ask_user live
python -m src --resume <id>       # continue a stopped session (rehydrated from its trajectory)
```

The REPL and `--resume` share one `ContextManager` + `Trajectory` across turns; `/exit`
ends the session, `/plan` prints the current plan. The one-shot path is what eval/Docker use.

or, containerized:

```
docker run --rm --env-file .env -v /path/to/repo:/workspace \
    openagent-code:latest "<task prompt>"
```

| Input | Source | Notes |
|---|---|---|
| Task prompt | CLI args (joined) | If empty, the CLI prompts for it on a TTY |
| Workspace | `CODE_WORKSPACE` | Dir the agent reads/edits; `/workspace` in the image |
| Model + endpoint | `CODE_MODEL`, `CODE_API_BASE`, `CODE_API_KEY` | The only outbound dependency |
| Verify command | `CODE_VERIFY_COMMAND` | Optional; the objective reward signal |

### 2.2 Exit codes

| Code | Meaning |
|---|---|
| `0` | Session `outcome == "success"` (and verify passed, if configured) |
| `1` | `outcome ∈ {verify_failed, error, max_steps}` |

### 2.3 Tools available to the model

| Tool | Mutating | Behavior |
|---|---|---|
| `read_file` | no | Returns content with line numbers; `offset`/`limit` for large files |
| `grep` | no | Regex over file contents; optional `glob` filter; capped at 200 hits |
| `glob` | no | Find files by glob pattern |
| `write_file` | yes | Create/overwrite a file |
| `edit_file` | yes | **Exact-match-or-fail**, requires a unique match unless `replace_all` |
| `run_command` | yes | Shell command (PowerShell on Windows, bash elsewhere); 120s timeout |
| `update_plan` | no | Record/update a tracked checklist for a multi-step task; pinned into the live context |
| `ask_user` | no | Ask the human a clarifying question (interactive mode); degrades to "proceed" when no human is present |
| `spawn_agent` | yes | Delegate a standalone subtask to an isolated subagent; returns its final answer. Capped by `CODE_MAX_SUBAGENT_DEPTH` |
| `remember` | no | Save a durable note to project memory (reloaded next session). **OPT-IN** (`CODE_MEMORY`); non-mutating (the agent's notebook), writes inside the fence |
| `web_fetch` | yes | Fetch a URL → text. **OPT-IN** (`CODE_ENABLE_WEB`); sends the URL off-machine. Only in the toolset when enabled |
| `web_search` | yes | Search via a BYO endpoint (`CODE_SEARCH_URL`). **OPT-IN**; sends the query off-machine |
| `mcp__<server>__<tool>` | varies | Dynamic — any tool from an MCP server in `CODE_MCP_CONFIG` (stdio). Discovered per run |

Every tool call is gated at dispatch by the permission engine (`src/permissions.py`,
§6.4): a `permission` record is written, then the call runs or is refused. The
"permission?" column above marks the *mutating* tools (those `plan` mode blocks and
`acceptEdits` auto-approves); read-only and notebook tools clear the gate once past
any `deny` rule and the workspace fence.

---

## 3. Outbound contract

openagent-code has exactly **one** outbound dependency: an OpenAI-compatible
chat-completions endpoint with tool/function-calling support, reached through
LiteLLM.

| Property | Value |
|---|---|
| Protocol | OpenAI Chat Completions (`tools`, `tool_choice="auto"`) |
| Endpoint | `CODE_API_BASE` (e.g. vLLM on RunPod) or Bedrock via `CODE_MODEL` |
| Auth | `CODE_API_KEY` (Bearer), or AWS credentials for Bedrock |
| Hard requirement | The model **must** support reliable tool-calling — the agent is ~90% tool calls |

No other network calls are made. The agent does not phone home; captured data
stays on the host (in the workspace) unless you move it.

---

## 4. State model

### 4.1 In-process state

| State | Lifetime | Notes |
|---|---|---|
| Message list | One task | The growing conversation passed to the model each step |
| `consecutive_fail` per tool | One task | Drives `retry_index` in the trajectory |
| Open trajectory file handle | One task | Flushed after every record |

There is no cross-task state in the process. Each invocation is independent.

### 4.2 Durable state — the trajectory file

One session == one JSONL file at `CODE_TRAJECTORY_DIR/<session_id>.jsonl`
(`schema_version` 0.5.0). Record types, in order of appearance:

| `type` | Emitted | Key fields |
|---|---|---|
| `session_start` | once, first | `schema_version`, `session_id`, `task`, `model`, `cwd`, `tool_schemas` (added in 0.2.0), `parent_session_id` + `depth` (links subagent runs; added in 0.4.0) |
| `session_resume` | on each resume | `session_id`, `ts` — marks where a stopped session was reopened (added in 0.5.0) |
| `turn` | per message added | `message` — the raw message, **never compacted**. The full history is the `turn` stream (added in 0.3.0) |
| `model_call` | per model step | `request.messages` (the **as-sent** view — possibly compacted), `request.as_sent`, `response.content/reasoning/tool_calls`, `usage`, `latency_ms` |
| `compaction` | when context overflows | `summarized_messages`, `summary`, `before_tokens`, `after_tokens` (added in 0.3.0) |
| `permission` | per gated tool call | `tool`, `target`, `allowed`, `action` (allow/ask/deny), `reason`, `rule`, `mode` — the decision, written just before the call (added in 0.6.0) |
| `tool_call` | per tool call | `tool`, `args`, `ok`, `retry_index`, `result` |
| `verification` | once, if configured | `command`, `ok`, `output` |
| `session_end` | once, last | `outcome`, `steps`, `completion_tokens_total`, `final_text`, `user_label` |

`user_label` is reserved for an accept/reject signal captured by a future UI; it
is `null` today. `model_call.request.messages` is the verbatim model input —
the field SFT/distillation consumes.

---

## 5. Configuration

Full reference in the README and `.env.example`. Contract-relevant values:

| Variable | Default | Contractual? |
|---|---|---|
| `CODE_MODEL` | `openai/gpt-oss-120b` | Yes — selects model + provider |
| `CODE_API_BASE` | `http://localhost:8000/v1` | Yes — the model endpoint |
| `CODE_API_KEY` | `EMPTY` | Yes — endpoint auth |
| `CODE_TOOL_MODE` | `native` | Yes — `native` (server tool-calls) or `json` (prompt fallback) |
| `CODE_WORKSPACE` | cwd / `/workspace` | Yes — what the agent edits |
| `CODE_MAX_STEPS` | `25` | Reference — loop cap |
| `CODE_MODEL_RETRIES` | `3` | Reference — retry transient errors + dropped tool calls (0 = off) |
| `CODE_REQUEST_TIMEOUT` | `600` | Reference — per-call read timeout; generous to absorb cold starts |
| `CODE_WARMUP` | `true` | Reference — probe-until-warm before the first task (off for Bedrock) |
| `CODE_WARMUP_BUDGET` | `120` | Reference — max seconds to wait for a cold worker to warm |
| `CODE_COMPACT_AT_TOKENS` | `12000` | Reference — live-context compaction budget (0 = off) |
| `CODE_MAX_SUBAGENT_DEPTH` | `1` | Reference — spawn_agent nesting cap (0 = off) |
| `CODE_ENABLE_WEB` | `false` | Yes — master switch for web_fetch/web_search (off = no egress, tools hidden) |
| `CODE_SEARCH_URL` | (empty) | Yes — BYO search endpoint for web_search |
| `CODE_MCP_CONFIG` | (empty) | Yes — path to MCP server config (stdio); tools appear as `mcp__*` |
| `CODE_VERIFY_COMMAND` | (empty) | Yes — defines the reward signal |
| `CODE_TRAJECTORY_DIR` | `trajectories` | Yes — where training data lands |
| `CODE_SFT_VIEW` | `raw` | Reference — `raw` or `as_sent` view the converter flattens |
| `CODE_AUTO_APPROVE` | `true` | Yes — back-compat shim for permission mode (true→`bypass`, false→`default`) |
| `CODE_PERMISSION_MODE` | (derived) | Yes — `default`/`acceptEdits`/`plan`/`bypass` (specs/0001-permissions.md) |
| `CODE_PERMISSIONS_CONFIG` | (empty) | Yes — JSON allow/ask/deny rules; `deny` always wins |
| `CODE_ADD_DIRS` | (empty) | Yes — extra roots the file tools may touch (widens the fence) |
| `CODE_MEMORY` | `false` | Yes — opt-in cross-session memory (specs/0002-memory.md); off keeps eval isolated |
| `CODE_MEMORY_FILE` | `.openagent/memory.md` | Reference — per-project memory file (relative to workspace) |
| `CODE_MEMORY_MAX_CHARS` | `4000` | Reference — cap on memory loaded into the system prompt |

---

## 6. Failure modes

### 6.1 Model endpoint unreachable / flaky
**Behaviour**: transient failures (connection/timeout/5xx) and dropped-tool-call
responses (empty content + no tool_calls from a worker missing the tool-call
parser) are **retried** `CODE_MODEL_RETRIES` times with backoff — so an
*intermittent* endpoint (some workers healthy, some not) still makes progress.
Only when all retries are exhausted does the turn fail: the CLI prints
`=== ERROR ===`, `outcome=error`, exit `1`; the eval harness marks that task
`error` and continues. Retried glitches are not logged (infra noise, not agent steps).
**Recovery**: a fully-broken endpoint needs the server fixed (uniform
`--enable-auto-tool-choice --tool-call-parser` across all workers); retries only
cover *intermittent* failure.

### 6.2 Model emits weak/invalid tool calls
**Behaviour**: malformed tool arguments are caught (`args = {}`); failing tools
return `ok=false` with a teaching message and `retry_index` increments. If the
model never converges, the loop stops at `CODE_MAX_STEPS` (`outcome=max_steps`).
**Common cause**: the served model is poor at function-calling — see §3.

### 6.3 Verification fails
**Behaviour**: `verification.ok=false`, `outcome=verify_failed`, exit `1`. The
trajectory is still complete and is a valuable *negative* training example.

### 6.4 Permission denied (the permission engine, Phase 4 #6)
**Behaviour**: every tool call is gated at dispatch by `permissions.decide(...)`
(specs/0001-permissions.md). A blocked call returns `Permission denied: <reason>`
as its tool result (a `permission` record captures the decision), and the model
gets that as teaching signal. Common causes: `plan` mode (read-only); a `deny` rule
(wins even under `bypass`); a path outside the workspace fence; or a `default`/`ask`
decision with no human present (headless can't prompt, so it blocks — never silently
allows). **Recovery**: pick the right `CODE_PERMISSION_MODE` (`bypass` for headless
auto), widen `CODE_ADD_DIRS`, or adjust the rules file. `CODE_AUTO_APPROVE=true`
(the default) maps to `bypass`, so out-of-the-box headless runs are unaffected.

### 6.5 Edit fails (not found / not unique)
**Behaviour**: `edit_file` returns `ok=false` with a message instructing the
model to re-read or add context. This is by design — it prevents silent
corruption and teaches the next attempt.

### 6.6 Cold start (scale-to-zero serverless endpoint)
**Behaviour**: a scale-to-zero worker (e.g. RunPod serverless) that has scaled to
zero spins a fresh worker on the first call after idle. During that window it
returns 200s with **empty tool_calls** until fully warm. Two mechanisms absorb
this: (1) every model call uses a generous `CODE_REQUEST_TIMEOUT` (600s) so the
spin-up isn't aborted — copied from openagent-infra, which absorbs the cold start
at call time; (2) `warm_up()` runs once before the first task — a throwaway
tool-call probe retried for up to `CODE_WARMUP_BUDGET` seconds until a real
tool_call comes back — so the first *real* task runs against an already-warm
worker. The warm-up never raises and is never logged (infra warm-up, not an agent
step). If the worker is still cold at the deadline it proceeds anyway, and the
per-turn `CODE_MODEL_RETRIES` cover any residual cold responses.
**Recovery**: none needed — this is absorbed automatically. Set `CODE_WARMUP=false`
to skip it (e.g. an always-warm endpoint or Bedrock, where it's already a no-op).

---

## 7. Operational characteristics

These are **expectations**, not guarantees. Measure in your own deployment.

| Property | Expected value |
|---|---|
| Image size | ~250–300 MB (slim + litellm + git) |
| Memory at idle | ~80–120 MB (the model runs remotely) |
| Wall-clock per task | Dominated by model latency, not the harness |
| Trajectory size | ~tens of KB to a few MB per session (full messages each step) |
| Disk growth | One JSONL per task; prune/scrub before training (see `train/`) |

---

## 8. Version history

| Version | Notes |
|---|---|
| 0.1.0 | Initial scaffold. Agent loop, six tools (exact-match edit), LiteLLM gateway (RunPod/Bedrock), schema-versioned trajectory capture, mandated verification, eval harness. Standalone CLI; not on openagent-network. Pre-production. |

---

## 9. Cross-references

- `README.md` — primary documentation, design rationale, the four layers
- `src/config.py` — every `CODE_*` variable the code reads (source of truth for §5)
- `src/tools.py` — the tool boundary (source of truth for §2.3)
- `src/trajectory.py` — the JSONL schema (source of truth for §4.2)
- `src/agent.py` — the loop
- `eval/harness.py` — the eval gate
- `train/convert.py` — consumes the §4.2 trajectory schema → SFT rows
- `train/README.md` — the converter + the training ladder
- `ROADMAP.md` — the committed build order and the phase gates
- `.env.example` — every variable the code reads

---

*openagent-code — part of the OpenAgent family, but runs standalone.*
