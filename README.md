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
│   └── DATASHEET.md          # the contract reference (invocation, schema, failure modes)
├── docker/
│   └── code/
│       └── Dockerfile        # CLI image (non-root, no port, no healthcheck)
├── docker-compose.yml        # one-shot `docker compose run` service
├── requirements.txt
├── .env.example              # every CODE_* variable, documented
├── src/                      # the agent
│   ├── config.py             # CODE_* env -> config (no YAML; .env is the source)
│   ├── model.py              # LiteLLM gateway (+ summarize() for compaction)
│   ├── tools.py              # tool boundary: read/grep/glob/write/edit/run_command/update_plan/spawn_agent/remember
│   ├── memory.py             # cross-session project memory — load + remember (Phase 4)
│   ├── context.py            # ContextManager — live context + compaction (Phase 4)
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

### Docker (the primary path)

```powershell
copy .env.example .env          # set CODE_API_BASE + CODE_API_KEY for your endpoint
docker compose build
docker compose run --rm openagent-code "add a docstring to foo.py and run the tests"
```

**Point it at a real repo** with `CODE_WORKSPACE` — the default `./workspace` is an
empty placeholder, and running against it produces a `no_action` outcome (nothing to edit):

```powershell
$env:CODE_WORKSPACE="C:\path\to\your\repo"
docker compose run --rm openagent-code "fix the failing test in foo.py"
```

Each run is labelled with an honest outcome in its trajectory:
`success` / `completed` / `verify_failed` / `no_action` / `protocol_stalled` /
`max_steps` / `error`. Only `success` and `completed` exit `0`.

### Local (dev)

```powershell
python -m venv .venv; .venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env          # edit CODE_API_BASE / CODE_API_KEY
python -m src "add an add(a,b) function to math_utils.py and run the tests"
```

Every run writes a trajectory to `trajectories/<session_id>.jsonl`.

### Interactive (multi-turn) & resume

```powershell
python -m src                     # REPL: a multi-turn chat session; ask_user is live
python -m src --resume <id>       # continue a stopped session, rehydrated from its trajectory
```

A one-shot run (`python -m src "task"`) is autonomous and deterministic. With no task
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
| `CODE_MODEL` | `openai/gpt-oss-120b` | LiteLLM model string (RunPod / Bedrock) |
| `CODE_API_BASE` | `http://localhost:8000/v1` | OpenAI-compatible endpoint (empty for Bedrock) |
| `CODE_API_KEY` | `EMPTY` | Endpoint key (or use `AWS_*` for Bedrock) |
| `CODE_TOOL_MODE` | `native` | `native` (server tool-calls) or `json` (prompt fallback) |
| `CODE_WORKSPACE` | cwd / `/workspace` | The repo the agent edits |
| `CODE_VERIFY_COMMAND` | (empty) | Objective reward, e.g. `pytest -q` |
| `CODE_MAX_STEPS` | `25` | Loop cap |
| `CODE_MODEL_RETRIES` | `3` | Retry transient errors + dropped tool calls (flaky-endpoint resilience) |
| `CODE_REQUEST_TIMEOUT` | `600` | Per-call read timeout (s) — generous, to absorb scale-to-zero cold starts |
| `CODE_WARMUP` | `true` | Probe-until-warm before the first task (no-op for Bedrock) |
| `CODE_WARMUP_BUDGET` | `120` | Max seconds to wait for a cold worker to warm |
| `CODE_COMPACT_AT_TOKENS` | `12000` | Compact the live context past this budget (0 = off) |
| `CODE_MAX_SUBAGENT_DEPTH` | `1` | How deep `spawn_agent` can nest (0 = off) |
| `CODE_ENABLE_WEB` | `false` | Opt-in `web_fetch`/`web_search` (off = no egress) |
| `CODE_MCP_CONFIG` | (empty) | Path to MCP server config; their tools appear as `mcp__*` |
| `CODE_SFT_VIEW` | `raw` | Converter view: `raw` (full history) or `as_sent` (compacted) |
| `CODE_AUTO_APPROVE` | `true` | Back-compat shim for permission mode (true→`bypass`, false→`default`) |
| `CODE_PERMISSION_MODE` | (derived) | `default` / `acceptEdits` / `plan` / `bypass` (Phase 4 #6) |
| `CODE_PERMISSIONS_CONFIG` | (empty) | JSON allow/ask/deny rules; `deny` always wins (see `permissions.json.example`) |
| `CODE_ADD_DIRS` | (empty) | Dirs the file tools may touch beyond the workspace (widens the fence) |
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
