# 0010 — Grounding gate (does the claim match the source?)

> Verified completion proves the agent DID the work; grounding proves the work is RIGHT. After the
> completion gate accepts a "done", the harness checks that the CLAIMS in the closing answer are
> grounded in the sources the agent cited/touched. Phase 10; the honesty line (specs/0007) extended.

## Why

A live centpilot ride made the gap concrete. Asked to align `docker/README.md` with the actual
compose/Dockerfiles, the agent honestly rewrote it (real diff, claim matched the change — verified
completion passed cleanly) but stated postgres init is `docker/database/init.sql` when the compose
actually mounts **`docker/auth/init.sql`**. That claim *changed a real file*, so it sailed straight
through the completion gate. **Verified completion catches lies; it cannot catch honest-but-wrong.**

## The gate

Runs in the same no-tool-call branch of `Agent.run`, immediately AFTER verified completion accepts
(and only then — an unverified run is already an honest failure). **Top level only** (`ctx.depth == 0`):
a subagent's answer is intermediate (the parent re-checks its own final synthesis), and a Tier-2
verifier must never grounding-check *itself* (its job is to quote paths, including ones it asserts are
absent). This also means the verifier can't trigger a verify-the-verifier cascade. Two tiers, in
`src/grounding.py`:

- **Tier 1 — deterministic, no model (the fallback when semantic is off).** `cited_paths(final)`
  extracts backtick/quote-wrapped tokens that end in a known file extension, deliberately EXCLUDING
  the look-alikes a bare slash would wrongly catch — import specifiers (`lodash/fp`), URLs/import hosts
  (`github.com/gorilla/mux`), scoped packages (`@scope/x`), dates (`2024/01/15`), and absolute/system
  paths. `deterministic_problems` flags a cited path with no evidence — not on disk AND not in the
  `ctx.mutations` ledger (so a file the agent legitimately deleted isn't a false phantom).
- **Tier 2 — semantic, harness-driven, `CODE_VERIFY_GROUNDING_SEMANTIC` (on by default), the
  AUTHORITY when on.** Spawns ONE **captured** verifier subagent (via `ctx.spawn`, the `run_subagent`
  path) that re-reads the cited sources *and any config that decides the real wiring*
  (compose/Dockerfile/manifest) and flags claims they don't support. Because it reads the workspace
  and judges, it doesn't false-flag an import/date/prose token the way a bare path-existence check
  would — so when semantic is on the deterministic tier is skipped entirely. It emits
  `UNGROUNDED: <claim> -> <what the file says>` lines (parsing tolerates markdown the model adds:
  `**UNGROUNDED**:`, `- UNGROUNDED:`); a lone `GROUNDED` passes. **Fail-open:** an empty/errored
  verdict is logged and treated as no-problem, so an infra hiccup never traps the agent in a re-prompt
  loop. **Firewall:** a verifier spawned inside an eval run inherits `ctx.traj_dir` = `trajectories/eval/`,
  so its captured trajectory stays behind the train/eval firewall (specs/0005), never leaking held-out
  eval content into the SFT corpus.

On a problem, the gate appends `grounding.challenge(...)` and re-prompts, up to
`CODE_VERIFY_GROUNDING_RETRIES`; exhausted, it returns the honest `ungrounded_completion` outcome.

**Change-claims ("I edited X") are deliberately not re-parsed** — the completion gate already checks
plan steps against the mutation ledger, and specs/0007 anchored on the *structured* plan (not prose)
to avoid brittle NL parsing. Grounding only checks cited-path *existence* (a literal extraction) and
semantic consistency (delegated to a subagent, never a regex).

## Why Tier 2 defaults ON

It is the more *agentic* check — and every verifier subagent is a first-class captured trajectory, so
running it produces training data. The "cost" of Tier 2 is corpus. That aligns with the flywheel.

## Outcome plumbing

`ungrounded_completion` is a new `terminated`/outcome, mirrored at ALL FOUR outcome-mapping sites:
`agent.RunResult`, `cli.py`, `subagent._classify`, and `eval/harness.run_agentic_task` (the last is
the site the completion gate also touches — miss it and the eval silently relabels it `completed`).
At each site the honest gate outcomes are checked BEFORE the `tool_calls==0 -> no_action` fallback —
grounding (unlike the completion gate, which needs an `update_plan` tool call) can fire with ZERO tool
calls (Tier 1, or Tier 2's verifier is a separate child), so a 0-tool-call ungrounded run must not be
mislabeled `no_action`. A fifth consumer, `eval/rubric.py`'s `verified_done`, caps an ungrounded run's
behavior score (same as unverified). It is NOT in `train/convert.KEEP_OUTCOMES`, so it is auto-dropped
from the SFT corpus like `unverified_completion`.

## Config

- `CODE_VERIFY_GROUNDING` (default `true`) — the gate.
- `CODE_VERIFY_GROUNDING_RETRIES` (default `2`) — bounded re-prompts.
- `CODE_VERIFY_GROUNDING_SEMANTIC` (default `true`) — Tier 2 (the verifier subagent).

## Files

- ADD `src/grounding.py` — the shared core (reused by Phase 11 curation).
- UPDATE `src/agent.py` — the gate, after completion accepts; `ground_retries`; the new outcome.
- UPDATE `src/config.py`, `.env.example` — the three knobs (1:1).
- UPDATE `src/cli.py`, `src/subagent.py`, `eval/harness.py` — the outcome mirror (all four sites),
  honest outcomes ordered before the `tool_calls==0` fallback.
- UPDATE `eval/rubric.py` — the `verified_done` session cap also fires on `ungrounded_completion`.
- UPDATE `src/subagent.py` + `eval/harness.py` — thread `ctx.traj_dir` so a verifier spawned inside an
  eval stays under `trajectories/eval/` (the firewall; also fixes any pre-existing eval subagent leak).
- UPDATE `src/trajectory.py` — the `session_end` outcome enumeration comment.
- ADD `scripts/check_grounding.py`, `specs/0010-grounding-gate.md`; UPDATE `ROADMAP.md`.

## Acceptance (`python scripts/check_grounding.py`)

- [ ] `cited_paths` pulls quoted local paths and EXCLUDES imports/URLs/dates/scoped/absolute tokens.
- [ ] A depth>0 ctx is skipped entirely (grounding is top-level only; the verifier can't self-ground).
- [ ] Semantic off: Tier 1 flags a cited path with no evidence; an existing or just-deleted path passes.
- [ ] Semantic on: the verifier is the authority (spawned, `UNGROUNDED:` parsed incl. `**UNGROUNDED**:`,
      `GROUNDED` passes, empty/`(subagent error` fails-open).
- [ ] Dep-free (no model, no network).

## Non-goals

- **Full NL claim parsing** — only cited *paths* are extracted deterministically; every other factual
  claim is checked by the Tier 2 subagent, not a regex.
- **Guaranteeing the verifier is right** — Tier 2 is the same base model; it can miss or (bounded by
  retries) false-flag. It raises the floor and banks corpus; it is not a proof.
- **Offline/eval semantic grounding** — Tier 2 needs a live workspace + spawn. The offline curator
  (Phase 11) and the `grounded_claims` rubric check stay deterministic.
