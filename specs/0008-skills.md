# 0008 — Skills (reusable, harness-orchestrated workflows)

> A skill is a Markdown file (`SKILL.md`) that packages a reusable agent workflow. C1 ships the
> system + one skill: a **decomposed code review** (review by concern, one captured subagent per
> concern). Referenced from OpenAI Codex's `.codex/skills/`; re-implemented as our own small Python.

## Why

Codex's `.codex/skills/` is the one pattern from that (huge, Rust) project worth adapting: reusable,
subagent-orchestrated workflows defined in Markdown. We already have the primitives (`spawn_agent`,
`review_repo`, `subagent.run_subagent`); a skills layer formalizes them into named, invokable,
**capturable** units. This is Phase C1 of the capability track (runs parallel to the Phase-8 wait).

The key decision — settled, not open: **the harness does the decomposition, not the model.** Codex's
orchestrator skill tells the *model* to "spawn one subagent per sub-skill." On this weak gpt-oss that
fails a new way every run — the exact failure that `src/orchestrator.py` was built to fix. So
`run_skill` fans out **in code**, mirroring `review_repo`. review_repo partitions by FOLDER;
`run_skill` partitions by CONCERN over one diff. Orthogonal; neither reinvents the other; review_repo
is untouched.

## The SKILL.md format

Directory-per-skill under a self-located `skills/` dir, mirroring Codex:
`skills/<name>/SKILL.md` = a `---`-fenced frontmatter block + a Markdown body. **Two kinds**,
distinguished by one optional frontmatter field:

- **Leaf** (a concern): frontmatter `name` + `description`; body = the concern's review rubric.
- **Orchestrator**: adds `subskills: code-review-*` — an `fnmatch` glob over sibling directory names;
  body = the synthesis/reduce rubric.

The glob (not a `concerns: [list]`) is deliberate: **scalar-only frontmatter**, fully data-driven —
dropping a new `skills/code-review-<x>/SKILL.md` adds a concern with **zero code change**, and the
glob `code-review-*` naturally excludes the `code-review` orchestrator itself (Codex's exact
semantic). Frontmatter is parsed by a small hand-rolled `key: value` reader (**not** PyYAML — keeps
`src/` yaml-free and never-raising); a malformed/missing block degrades to `name=<dirname>`, empty
description, whole file as body, and **never raises** (same posture as `config.load_permission_rules`).

Bundled `scripts/` / `references/` dirs beside `SKILL.md` are **format-supported**; C1 shipped none,
**C2 adds the first** (`review-log`'s `summarize_log.py` — see "Skill + script bundling" below).

## Loader — `src/skills.py`

A thin sibling of `orchestrator.py` with the same import discipline: imports only `config` (+ stdlib
`os`, `fnmatch`, `subprocess`) at module top; imports `ToolResult` **lazily inside `run_skill`** to
avoid the tools↔skills cycle (verbatim the trick `orchestrator.py` uses).

- `config.skills_dir()` → absolute dir: `CODE_SKILLS_DIR` (default `"skills"`) as-is if absolute,
  else `os.path.join(config.INSTALL_ROOT, d)`. A **line-for-line clone of `config.trajectory_dir()`**
  (not `_resolve_install_path`, which is file-only). This is the self-locating requirement: `oac`
  finds the skills corpus from **any** repo.
- `Skill` dataclass — `name, description, body, meta: dict, dirname, path`.
- `parse_skill(text, dirname, path)` → `Skill`. Splits the leading `---…---`, scalar parse, rest =
  body. Never raises.
- `load_skill(name)` → `Skill | None` for `skills_dir()/<name>/SKILL.md`.
- `list_skills()` → `[Skill]` over immediate subdirs holding a `SKILL.md` (for prompt advertising +
  self-documenting errors). Never raises.
- `find_subskills(skill)` → `[Skill]` whose dirname matches `skill.meta["subskills"]` via `fnmatch`,
  **excluding the orchestrator's own dir** — the Pythonic "all `code-review-*` other than this one."
- `_current_diff(cwd)` → `(diff_text, changed_files)` or `(None, reason)`. Runs `git diff HEAD`
  (fallback `git diff`) + `--stat` via `subprocess` in `cwd`; try/except; never raises; returns a
  reason when `cwd` isn't a git repo / there's no diff. Truncated at a module constant (~20k chars).
- `run_skill(args, ctx)` → `ToolResult` — the dispatcher (lives here so `tools.py` stays a thin
  boundary, exactly as `review_repo` lives in `orchestrator.py`).

## Invocation

Native tool `run_skill(name, target?)`, registered as a **gated group `SKILL_TOOLS`** in `tools.py`
and joined in `toolset.active_tools()` via `if config.SKILLS: tools += SKILL_TOOLS` — beside the
`MEMORY_TOOLS` / `WEB_TOOLS` gates. `tools.py` adds `from .skills import run_skill` next to the
existing `from .orchestrator import review_repo` (after `ToolResult` is defined → no cycle).
`native_tools_note` advertises it automatically; `prompts.py` adds one short note listing skill
names+descriptions when `run_skill` is active (mirrors the web note).

`run_skill` dispatches on skill **shape** (harness-driven, like `review_repo`):
- **unknown/empty name** → teaching `ToolResult(False)` listing `list_skills()` names.
- **leaf** (no `subskills`) → `ToolResult(True, skill.body)`: inline guidance, no subagent.
- **orchestrator** (has `subskills`) → deterministic concern fan-out, structurally identical to
  `review_repo`:
  1. guard `ctx.spawn is None` and `ctx.depth >= 1` (a concern-child can't re-fan) — same guards as
     `review_repo`.
  2. compute the diff **once** via `_current_diff(ctx.cwd)`; if none, return a clean explanation
     (children never confabulate a review of a non-existent diff).
  3. cap `subskills` at `config.MAX_REVIEW_AREAS` (reused — already "harness review fan-out cap").
  4. for each concern: `ctx.spawn(child_task)` where `child_task` embeds the harness-computed diff +
     changed-file list + that leaf's body: *"Review THIS diff for `<concern>`. `<leaf body>`. You MAY
     read_file for surrounding context. Report NUMBERED findings, each with file:line. READ-ONLY."*
     Reuses `subagent.run_subagent` (not the `spawn_agent` tool) → each concern is a captured child.
  5. reduce: concat the per-concern findings, append the orchestrator body as the synthesis rubric,
     plus a hardcoded "this is your final review, no more tool calls" guard (like review_repo's
     footer). Return `ToolResult(True, digest, {"concerns": n})`.

## The first skill — decomposed code review

One orchestrator + three concern leaves (matching the repo's own review vocabulary):
- `skills/code-review/SKILL.md` — `name: code-review`, `subskills: code-review-*`; body = the
  synthesis rubric (merge every finding into ONE numbered report, each with file:line; return them
  all; read-only; don't re-read or call more tools).
- `skills/code-review-correctness/SKILL.md` — logic bugs, edge cases, error handling, `ok=False`
  contracts introduced by the diff.
- `skills/code-review-tests/SKILL.md` — does changed logic add/adjust a pytest under the test dir?
  flag untested changed behavior (adapts Codex `code-review-testing`).
- `skills/code-review-breaking-changes/SKILL.md` — this repo's real external surfaces: tool JSON
  schemas in `tools.py`, `CODE_*` env-var names/defaults in `config.py`, permission-rule matchers,
  and the trajectory/JSONL shape the flywheel converter reads (adapts Codex `code-review-breaking-changes`).

## Skill + script bundling (C2)

A skill dir may bundle helper scripts under `scripts/`. `bundled_scripts(skill)` returns their
absolute paths; a LEAF skill's `run_skill` return appends them (and any `target`) so the model can
run them via `run_command` / read them via `read_file`. The first such skill is **`review-log`** — a
leaf that reviews an openagent-code session log, bundling `summarize_log.py` (extracts the log's
signals — tool counts, fails, retries, compactions, completion-challenges, reasoning-leak, `.env`
touches, thrash — into a bounded digest the reviewer confirms against the log). This proves the
platform generalizes past the orchestrator shape and exercises the Codex `babysit-pr` skill+script
pattern. The summarizer is **stdlib-only and defensive** (an unrecognized log line is ignored).

## Config

In `src/config.py`, alongside the MEMORY (opt-in gate) and trajectory (self-location) blocks:
- `SKILLS = _as_bool(os.environ.get("CODE_SKILLS", "false"))` — master gate, **off by default**
  (opt-in, like `CODE_MEMORY`). Off → `run_skill` isn't offered to the model at all.
- `SKILLS_DIR = os.environ.get("CODE_SKILLS_DIR", "skills")`.
- `skills_dir()` — verbatim clone of `trajectory_dir()`.
- **No new fan-out knob** — reuse `config.MAX_REVIEW_AREAS`.

`.env.example` documents `CODE_SKILLS` / `CODE_SKILLS_DIR` (mirroring the `CODE_MEMORY` block).

## Flywheel fit

**Zero new capture code** — it rides `subagent.run_subagent`: each concern child opens its own
`Trajectory` linked by `parent_session_id` + `depth`, is `_classify`'d and `.end()`'d, and is written
to the one corpus at `config.trajectory_dir()` that `train/convert.py` reads. One
`run_skill(code-review)` → the lead trajectory + N concern trajectories = **N+1 SFT rows**, each a
focused, gradeable concern review ("subagents multiply the dataset"). Because the harness **embeds the
computed diff** into each child's task, that diff is logged in the child's `session_start.task`, so
every row is self-contained (input diff + concern rubric + findings) with **zero schema change**, and
is filterable by concern via the task text. The rubrics + reduce footer demand exactly the grounded,
file:line, no-confab, read-only discipline the Phase-7 behavior eval scores and
`looks_like_reasoning_preamble` filters — high-signal review training data by construction.

## Files

- **ADD** `src/skills.py`, `skills/code-review/SKILL.md`, `skills/code-review-correctness/SKILL.md`,
  `skills/code-review-tests/SKILL.md`, `skills/code-review-breaking-changes/SKILL.md`, this spec.
- **UPDATE** `src/config.py` (gate + `skills_dir()`), `src/tools.py` (`SKILL_TOOLS` + lazy import),
  `src/toolset.py` (gated group), `src/prompts.py` (skills note), `.env.example` (the two knobs).
- **ADD (acceptance)** `scripts/check_skills.py` — dep-free: skills load/parse, `find_subskills` glob
  excludes the orchestrator, `run_skill` teaching-error on unknown name, `skills_dir()` self-locates.
- **DELETE** none.

## Decisions made (were the synthesizer's open questions — defaults chosen, override on review)

1. **Frontmatter parser:** hand-rolled scalar `key: value` (not PyYAML). PyYAML *is* a dep but `src/`
   imports it nowhere; a hand-rolled never-raise reader keeps `skills.py` as import-disciplined as
   `orchestrator.py`. *(Override → `yaml.safe_load` if you'd rather match `eval/tasks`.)*
2. **Diff command:** `git diff HEAD` (all uncommitted vs last commit) with a `git diff` fallback.
   *(Override → `--staged` only, or `origin/main`.)*
3. **Leaf discoverability:** advertise only the `code-review` orchestrator; leaves stay invokable by
   name but aren't listed. *(Override → advertise concerns individually.)*
4. **Fan-out cap:** reuse `MAX_REVIEW_AREAS`. *(Override → a dedicated `CODE_MAX_SKILL_CONCERNS`.)*
5. **Concern naming:** `code-review-tests` (repo's "tests" vocabulary), not Codex's
   `code-review-testing`.

## Acceptance (checkable)

- [ ] `CODE_SKILLS=false` (default) → `run_skill` is **not** in `active_tools()`; agent unchanged.
- [ ] With `CODE_SKILLS=true`, `run_skill("code-review")` in a repo with a diff fans out one captured
      subagent per concern and returns one numbered synthesis; with no diff, it says so and spawns none.
- [ ] `run_skill` on an unknown name returns a teaching error listing available skills.
- [ ] `skills_dir()` resolves against `INSTALL_ROOT` (skills found when run from another repo).
- [ ] `find_subskills` excludes the orchestrator; a new `code-review-<x>/` adds a concern with no code
      change.
- [ ] `scripts/check_skills.py` passes with no model/network.

## Non-goals (C1)

- **Model-driven decomposition** (the model spawning the subagents) — rejected; the harness fans out.
- A generic skill **marketplace / discovery UI** or dynamic skill installation — beyond the current
  file-based library (bundled helper scripts, run by the model via `run_command`, are supported).
- **A trajectory schema bump** for a structured `skill`/`concern` tag — the concern is already in the
  child's task text; a structured tag is a later option, not C1.
