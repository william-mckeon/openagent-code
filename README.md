# openagent-code

> A self-hosted coding agent you fully control — your model, your infra, your
> data — instrumented from the first line to produce training data about its own
> work. Part of the OpenAgent family, but deliberately **standalone**: it joins
> no network and depends on no other OpenAgent service.

**Maintainer:** William McKeon ([github.com/william-mckeon](https://github.com/william-mckeon))  ·  **Status:** working — pre-production  ·  Apache 2.0 License © 2026 William McKeon

---

## What this is

A terminal coding agent — read files, edit them, run commands, verify — that
runs on a model **you** host (gpt-oss-120b on RunPod, or Bedrock) and captures
every session as structured training data. The bet: a large share of a coding
agent's proficiency lives in the *harness*, not the model. So openagent-code
puts a sharp harness around a model you own, and logs each run so the model can
get better at *your* tools and *your* tasks over time.

Unlike the HTTP services in the OpenAgent system (openagent-api/-infra/-logger/
-memory), this is a **one-shot CLI**: it runs a single task on a target repo,
writes a trajectory, and exits. It is not on `openagent-network`, exposes no
port, and reaches its model endpoint directly. It adopts the OpenAgent house
conventions for structure, Docker, and docs — not its runtime topology.

---

## The four layers

Each layer talks to the next through a stable interface, so any one can be
swapped without touching the others.

```text
  serving        your model behind an OpenAI-compatible endpoint (vLLM / Bedrock)
     │
  src/model.py   LiteLLM gateway — swap RunPod <-> Bedrock via CODE_* env, never code
     │
  src/tools.py   the tool boundary: read (line numbers), edit (exact-match-or-fail),
     │           grep, glob, run_command — ergonomics that make the agent proficient
     │           AND emit ok/fail + retry signal
  src/agent.py   the loop: model decides -> run tools -> verify -> repeat
     │
  src/trajectory.py  every session -> schema-versioned JSONL in trajectories/
```

Proficiency and trainability are the *same* design: the discipline that makes
the agent good (exact-match edits, mandated verification, report-faithfully) is
exactly what produces clean labels for training.

On top of the loop (Phase 4): **context compaction** (summarize-and-continue so
long runs don't blow the window), **subagents** (`spawn_agent` — isolated and
separately captured), **planning** (`update_plan` — a pinned checklist), a
**permission engine** (modes + allow/ask/deny rules + a workspace fence, gated at
dispatch — see [`specs/0001-permissions.md`](specs/0001-permissions.md)), and
**cross-session memory** (`remember` — per-project notes reloaded each session, see
[`specs/0002-memory.md`](specs/0002-memory.md)). See [`ROADMAP.md`](ROADMAP.md).

---

## Repo layout

```text
openagent-code/
├── README.md                 # this file
├── ROADMAP.md                # the committed build order + phase gates
├── docs/
│   ├── DATASHEET.md          # the contract reference (invocation, schema, failure modes)
│   └── VALIDATION.md         # run-after-every-phase checks (harnesses + live-ride query menu)
├── docker/
│   └── code/
│       └── Dockerfile        # CLI image (non-root, no port, no healthcheck)
├── docker-compose.yml        # one-shot `docker compose run` service
├── requirements.txt
├── .env.example              # every CODE_* variable, documented
├── src/                      # the agent
│   ├── config.py             # CODE_* env -> config (no YAML; .env is the source)
│   ├── model.py              # LiteLLM gateway (+ summarize() for compaction)
│   ├── tools.py              # tools: read/grep/glob/tree/write/edit/delete/run_command/update_plan/spawn_agent/review_repo/run_skill/run_workflow/apply_patch/request_dir/remember
│   ├── editmatch.py          # safe fuzzy fallback for edit_file (specs/0013)
│   ├── patch.py              # atomic multi-file apply_patch (specs/0013)
│   ├── verify_edits.py       # auto-verify: run a check on touched files + reflection loop (specs/0014)
│   ├── execpolicy.py         # parse run_command into segments, classify read-only/mutating/dangerous (specs/0016)
│   ├── sandbox.py            # FS confinement: fence run_command's writes to the workspace (specs/0017)
│   ├── guardian.py           # fail-closed LLM approval reviewer for the ask tier (specs/0019)
│   ├── hooks.py              # opt-in, fail-open PreToolUse/PostToolUse/PermissionRequest hooks (specs/0015)
│   ├── goal.py               # goal loop: pursue a machine-checkable bar; the bar decides done (specs/0020)
│   ├── effort.py             # adaptive reasoning effort: pluggable policy, escalate-only (specs/0021)
│   ├── effort_online.py      # opt-in self-learning effort policy (CODE_EFFORT_POLICY=online, specs/0021)
│   ├── skills.py             # skills (specs/0008): SKILL.md workflows; run_skill fans out captured concern subagents
│   ├── workflow.py           # workflows (specs/0038): run_workflow — a multi-phase fan-out+reduce engine
│   ├── fanout.py             # bounded parallel fan-out (specs/0039): one helper for all three fan-out sites
│   ├── tasks.py              # async background runtime (specs/0040): pure TaskRegistry + drain/fold + subprocess launcher
│   ├── memory.py             # cross-session project memory — load + remember (Phase 4)
│   ├── context.py            # ContextManager — live context + compaction (Phase 4)
│   ├── envcontext.py         # per-turn environment block — situational context (specs/0012)
│   ├── planner.py            # native vs json tool-calling protocols
│   ├── agent.py              # the loop
│   ├── subagent.py           # spawn_agent runner — nested, captured subagents (Phase 4)
│   ├── runtime.py            # build_agent wiring
│   ├── trajectory.py         # JSONL capture (raw history + as-sent views)
│   ├── prompts.py            # system prompt (behavioral scaffolding)
│   ├── permissions.py        # permission engine — modes + rules + fence (Phase 4)
│   └── cli.py                # `python -m src "task"`
├── eval/
│   ├── harness.py            # the eval gate — pass-rate on held-out tasks
│   └── tasks/                # *.yaml: prompt + setup + verify
├── train/
│   ├── convert.py            # trajectories -> SFT rows (`python -m train.convert`)
│   └── README.md             # the converter + the training ladder
├── specs/                    # spec-driven development (specs are done-criteria)
├── trajectories/             # captured sessions (git-ignored)
└── workspace/                # default mount point for the repo being edited
```

---

## Quickstart

### Local (the interactive path)

Install once, then `cd` into any repo and just run it — the workspace defaults to the
current directory, and the common knobs are flags, so there's no `CODE_*` env juggling:

```powershell
python -m venv .venv; .venv\Scripts\Activate.ps1
pip install -e .                # puts the `openagent-code` command on your PATH
copy .env.example .env          # set CODE_API_BASE + CODE_API_KEY for your endpoint

cd C:\path\to\your\repo
openagent-code "fix the failing test in foo.py"     # one-shot, autonomous
openagent-code                                       # interactive REPL (just talk to it)
```

**Name it (optional, specs/0036–0037).** The agent is **OAC** by default. Give it your own name once, at install:

```powershell
openagent-code --set-name arcus --persona "precise, direct, a little wry"
# writes CODE_AGENT_NAME/CODE_AGENT_PERSONA to .env, generates scripts\arcus.ps1, and REGISTERS it in your
# PowerShell $PROFILE — reload (. $PROFILE) or open a new shell, then `arcus` launches it.
#   add --no-profile to skip the $PROFILE step and just print the line to add by hand.
openagent-code --remove-name        # revert name + persona to OAC, remove the launcher + its $PROFILE line
```

The package/import name stays `openagent-code`; only the agent's display identity + an added launcher change.

Common flags (instead of `$env:CODE_*`):

| Flag | Purpose |
|---|---|
| `-C` / `--workspace <path>` | The repo to work in (default: current directory) |
| `--mode <name>` | Permission mode: `default` / `acceptEdits` / `plan` / `bypass` |
| `--add-dir <path>` | Grant a *reference* folder beyond the workspace (repeatable) |
| `--memory` / `--no-memory` | Toggle cross-session memory for this run |
| `--warmup <seconds>` | Cold-start warm-up budget |

e.g. review another repo read-only while it stays out of harm's way:
`openagent-code -C C:\my\project --mode plan --add-dir C:\other\repo`.
Each run is labelled with an honest outcome (`success` / `completed` / `verify_failed` /
`no_action` / `protocol_stalled` / `max_steps` / `goal_unmet` / `error`); only `success`/`completed`
exit `0`.

### Docker (the sandbox / eval path)

Docker is the **isolated, reproducible** runtime — the eval harness and CI, or running an
untrusted task in a box. It deliberately sees only the one mounted `/workspace`, so for
roaming your own machine use the local path above.

```powershell
copy .env.example .env
docker compose build
docker compose run --rm openagent-code "add a docstring to foo.py and run the tests"
# point at a real repo: $env:CODE_WORKSPACE="C:\path\to\repo"  before the run
```

Every run writes a trajectory to `trajectories/<session_id>.jsonl`.

### Interactive (multi-turn) & resume

```powershell
openagent-code                    # REPL: a multi-turn chat session; ask_user is live
openagent-code --resume <id>      # continue a stopped session, rehydrated from its trajectory
```

(`python -m src ...` works identically if you'd rather not `pip install -e .`.)
In the REPL, `/plan` shows the plan, **`/add-dir <path>`** grants a reference folder
mid-conversation (no restart), and **`/mode <name>`** switches permission mode on the fly.
A one-shot run (`openagent-code "task"`) is autonomous and deterministic. With no task
you get a REPL — `/exit` ends it (and prints the `--resume <id>` to continue later),
`/plan` shows the current plan. Resume works because the trajectory **is** the saved
session: it's rehydrated from the raw `turn` records, not a separate state file.

---

## The flywheel

1. **Capture** — `src/trajectory.py` logs at the model gateway and the tool
   boundary (the two stable seams), so the harness can change without breaking
   the dataset.
2. **Reward** — `tool_call.ok` + `retry_index` (cheap), `verification.ok`
   (objective — set `CODE_VERIFY_COMMAND`), `session_end.user_label`
   (accept/reject, gold; reserved).
3. **Eval** — `python -m eval.harness` runs held-out tasks in sandboxes; pass
   rate is the gauge that tells you a new model is actually better.
4. **Convert** — `python -m train.convert` filters winning trajectories and
   flattens them into SFT rows (`train/dataset/`). See `train/README.md`.
5. **Train** — SFT on wins → rejection sampling on test-pass → DPO → RL. Scrub
   secrets/PII and decontaminate first.

The committed build order and the reasoning behind it live in
[`ROADMAP.md`](ROADMAP.md) — read that before picking up the next phase.

---

## Configuration

All config is `CODE_*` environment variables (read in `src/config.py`, defaulted
there, documented in `.env.example`). There is no YAML config file. Key ones:

| Variable | Default | Purpose |
|---|---|---|
| `CODE_MODEL` | `openai/gpt-oss-120b` | LiteLLM model string (RunPod / Bedrock / Together / Tinker) |
| `CODE_API_BASE` | `http://localhost:8000/v1` | OpenAI-compatible endpoint (empty for Bedrock) |
| `CODE_AGENT_NAME` | `OAC` | The name the agent answers to (identity line + banners); manage with `--set-name` / `--remove-name` |
| `CODE_AGENT_PERSONA` | (empty) | Optional one-line persona appended to the system prompt; empty appends nothing |
| `CODE_API_KEY` | `EMPTY` | Endpoint key (or use `AWS_*` for Bedrock) |
| `CODE_TOOL_MODE` | `native` | `native` (server tool-calls) or `json` (prompt fallback) |
| `CODE_WORKSPACE` | cwd / `/workspace` | The repo the agent edits |
| `CODE_VERIFY_COMMAND` | (empty) | Objective reward, e.g. `pytest -q` |
| `CODE_MAX_STEPS` | `25` | Loop cap |
| `CODE_MODEL_RETRIES` | `3` | Retry transient errors + dropped tool calls (flaky-endpoint resilience) |
| `CODE_REQUEST_TIMEOUT` | `600` | Per-call read timeout (s) — generous, to absorb scale-to-zero cold starts |
| `CODE_WARMUP` | `true` | Probe-until-warm before the first task (no-op for Bedrock) |
| `CODE_WARMUP_BUDGET` | `120` | Max seconds to wait for a cold worker to warm |
| `CODE_STREAM` | `false` | Stream the primary model turn (specs/0043); off = byte-identical single call |
| `CODE_SHELL_HINTS` | `false` | Append PowerShell 5.1 command rules to the env block on Windows (specs/0046); needs `CODE_SITUATIONAL_CONTEXT` |
| `CODE_GROUND_SKIP_GREENFIELD` / `CODE_GROUND_GREENFIELD_MAX` | `false` / `0` | Skip path-grounding on an empty (specs/0042) or small early-stage (specs/0047) workspace; `MAX` = file-count threshold |
| `CODE_REASONING_PARAM` / `_VALUE` / `_TOPLEVEL` | `reasoning_effort` / (empty) / `false` | Reasoning pass-through (specs/0044); VALUE empty = off (legacy `CODE_REASONING_EFFORT` path) |
| `CODE_EXTRA_BODY` | (empty) | Extra JSON params merged into the request `extra_body` (specs/0049); e.g. `{"separate_reasoning": true}` for Tinker; empty = off |
| `CODE_COMPACT_AT_TOKENS` | `12000` | Compact the live context past this budget (0 = off) |
| `CODE_MODEL_MAX_TOKENS` | `131072` | Model's HARD context window — sent context compacted under it (specs/0034); `auto` resolves it at startup (specs/0045) |
| `CODE_MODEL_MAX_OUTPUT_TOKENS` / `CODE_OUTPUT_MARGIN_TOKENS` | (empty) / `4096` | Optional per-request output cap (specs/0045); empty = no cap (byte-identical); `auto` = window − prompt − margin |
| `CODE_MAX_SUBAGENT_DEPTH` | `1` | How deep `spawn_agent` can nest (0 = off) |
| `CODE_ENABLE_WEB` | `false` | Opt-in `web_fetch`/`web_search` (off = no egress) |
| `CODE_MCP_CONFIG` | (empty) | Path to MCP server config; their tools appear as `mcp__*` |
| `CODE_SFT_VIEW` | `raw` | Converter view: `raw` (full history) or `as_sent` (compacted) |
| `CODE_AUTO_APPROVE` | `true` | Back-compat shim for permission mode (true→`bypass`, false→`default`) |
| `CODE_PERMISSION_MODE` | (derived) | `default` / `acceptEdits` / `plan` / `bypass` / `propose` (Phase 4 #6) |
| `CODE_PROPOSE_RUN_AFTER_APPROVAL` / `_EXTEND_AFTER_APPROVAL` / `_PERSIST_APPROVAL` | `false` | Propose follow-through (specs/0048): run/test, prompted-extend, or scoped-bypass after the first approval; deny+fence always win |
| `CODE_GUARD_SELF_KILL` | `false` | Self-preservation (specs/0050): hard-deny a name-based process kill that would end the agent's own `python` process, in every mode incl. bypass |
| `CODE_PERMISSIONS_CONFIG` | (empty) | JSON allow/ask/deny rules; `deny` always wins (see `permissions.json.example`) |
| `CODE_ADD_DIRS` | (empty) | Dirs the file tools may touch beyond the workspace (widens the fence) |
| `CODE_TRUST_USER_DIRS` | `false` | Treat a dir the user literally types as a **read** grant, and auto-grant `request_dir` for an existing dir under bypass at depth 0 (into a read-only tier writes can't reach) |
| `CODE_WORKFLOWS` | `false` | Offer `run_workflow`: a multi-phase fan-out+reduce engine (specs/0038). `CODE_MAX_WORKFLOW_PHASES` caps the pipeline length |
| `CODE_WORKFLOW_CONCURRENCY` | `1` | Bounded parallel fan-out (specs/0039): 1 = serial (byte-identical); N = up to N children at once across `run_workflow`/`review_repo`/`run_skill`. Parallel children run **read-only** |
| `CODE_REPLY_SHAPE` | `false` | An explicit user reply-shape/length instruction this turn (e.g. "respond with only Yes") outranks a tool's "synthesize now" trailer, and is per-turn (specs/0041) |
| `CODE_WORKFLOWS_ASYNC` | `false` | REPL-only (specs/0040): `run_workflow` can **submit** to run in the background + return a task-id; `/tasks`, `/result <id>`, a completion banner. Workers are read-only |
| `CODE_MAX_BACKGROUND_TASKS` | `3` | Cap on concurrent background tasks |
| `CODE_MEMORY` | `false` | Opt-in cross-session memory: offer `remember`, load project notes into context |
| `CODE_MEMORY_FILE` | `.openagent/memory.md` | Per-project memory file (relative to the workspace) |
| `CODE_MEMORY_MAX_CHARS` | `4000` | Cap on memory loaded into the system prompt |

---

## Data sovereignty

Self-hosted vLLM: prompts/code never reach a model vendor. Bedrock: stays in
your AWS account/region, not used to train anyone's model. Switch between them
by editing `.env` only. `.gitignore` keeps `.env` and captured trajectories out
of git by default — but trajectories can contain source from the repos you work
on, so treat `trajectories/` and `train/` data with the same care as secrets.

**Web search** is the same choice at the tool layer. `CODE_SEARCH_PROVIDER=searxng`
against a self-hosted SearXNG is the data-sovereign path — the query leaves only
your infra. `tavily` is the convenience path (hosted, a synthesized answer + ranked
sources) at the cost of hosted egress; Tavily's MCP server (`mcp.json.example`, marked
`"web": true`) adds `extract`/`crawl`/`research` on top, routed through the same
untrusted-content fence + grounding ledger as the native tools (specs/0029).

---

## Status & honest gaps

Validated end-to-end on self-hosted **gpt-oss-120b** with native tool-calling: the
agent runs the investigate→fix→verify loop and the eval passes **13/13**. Built and
working — the eight tools, LiteLLM gateway, trajectory capture (schema 0.4.0), the
eval harness, the SFT converter (`python -m train.convert`), **context compaction**,
**subagents** (`spawn_agent`), and **planning** (`update_plan`).

The eval now spans two tiers: an **easy regression tier** (8 single-edit tasks) and
a **discriminating tier** (5 harder tasks — multi-file rename, coordinated two-edit,
boundary edge-cases, multi-fix planning, regression-guard) whose verifies are sharp
enough to reject a *plausible-but-wrong* fix (a missed call site, a hard-coded value,
an ignored boundary). The model clears the harder tier too (13/13), but it visibly
*works harder* for it (12–15 tool calls vs 6–9). So the suite is now more
discriminating by design; finding the model's actual ceiling needs a still-harder
tier — the next eval calibration, see [`ROADMAP.md`](ROADMAP.md).

Not yet built (see [`ROADMAP.md`](ROADMAP.md), Phase 4): permission **hooks** (the
programmable `PreToolUse`/`PostToolUse` second pass — the Core engine of
modes/rules/fence is built), and the accept/reject capture that fills `user_label`.
The harder eval
tier sharpened the verifies but the pass-rate is still pinned at 100% — a
genuinely-hard tier (to find where the model breaks) is the next calibration so the
number can finally move. Native tool-calling needs the vLLM worker launched with
`--enable-auto-tool-choice --tool-call-parser`; `CODE_TOOL_MODE=json` is the
portable fallback.

---

*openagent-code — part of the OpenAgent family, but runs standalone.*
