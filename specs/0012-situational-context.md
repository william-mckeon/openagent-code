# 0012 — Situational-context injection

> A small, per-turn block of the agent's real environment — cwd, OS, shell, today's date, granted
> reference dirs, and (opt-in) the git branch + a bounded status — injected fresh each turn so the
> model conditions on live state instead of confabulating it. Phase 12 of the adoption track (ROADMAP).

## Why

`src/prompts.py` `BASE_PROMPT` is a static constant: it carries **no runtime state**. So the agent
never sees its own cwd, OS, shell, the date, or which git branch it is on — it has to guess, and a
weaker model guesses wrong (a confabulated date, a relative path resolved against the wrong root, an
edit against a stale branch). Every other capable agent injects a per-turn environment block; we do not.

This is the cheapest capability win on the adoption track and it directly serves the flywheel: a
captured trajectory that carries the real environment teaches the student model to *condition on
situational state* — the "orient before acting" behavior `BASE_PROMPT` preaches but currently cannot
ground in any live signal. A per-turn git line also cheaply covers part of the file-staleness concern.

## The design

The block is **dynamic** state, so it must NOT go in `BASE_PROMPT` (logged once as the first raw turn
and server-cached — a date/branch there would pin stale for the whole session). Instead it is a
**refreshed pin**: `ContextManager.set_env_context(text)` stores it in the never-compacted `_base()`
region (like `pinned_task` / `pinned` / `pinned_review`) and **replaces** it on every call, so it is
always sent, never summarized away, and never stale.

- **`src/envcontext.py`** (new) — `build_env_context(cwd, granted_dirs, include_git, git_status_fn, now)`
  returns a bounded plain-text block. Pure and injectable (`now`, `git_status_fn`) so the acceptance
  harness is deterministic and shells out to nothing. Only stdlib; **never raises**. `_format_git`
  (pure) formats a bounded `branch | N changed (files…)` line from a canned porcelain; `_git_status`
  runs git (`rev-parse --abbrev-ref HEAD` + `status --porcelain`) with a `shutil.which` guard and a
  timeout, returning `None` on a non-repo / missing git / any error.
- **`src/context.py`** — a `pinned_env` slot + `set_env_context(text)` (bounded via `_capped`,
  specs/0009), added to `_base()`.
- **`src/agent.py`** — once per `run()` (after `set_task`, the single choke point for one-shot / REPL),
  when `config.SITUATIONAL_CONTEXT` is on: build the block and `set_env_context(...)` (the pin) **and**
  `cm.add(...)` a logged copy — the same dual pattern `set_review_digest` uses, so the block lands in
  both the compaction-safe pin AND the raw turn stream the converter reads (the task itself is already
  doubled the same way: `pinned_task` + the working task message).
- **`src/config.py`** — `SITUATIONAL_CONTEXT` (P1) and `SITUATIONAL_GIT` (P2), both `CODE_*`, default
  **off**; `SITUATIONAL_GIT` is only consulted when `SITUATIONAL_CONTEXT` is on (gated at the call site).

## Sub-phases (each independently shippable, behind its flag)

- **P1 — env block + refreshed pin** (`CODE_SITUATIONAL_CONTEXT`): cwd / OS / shell / date / granted
  dirs. Zero subprocess cost. Acceptance: `scripts/check_situational.py`.
- **P2 — per-turn git branch + bounded status** (`CODE_SITUATIONAL_GIT`): appends the git line; one git
  call per turn (not per step), degrades cleanly on a non-repo. Acceptance:
  `scripts/check_situational_git.py`.

## Acceptance (checkable)

`python scripts/check_situational.py` (dep-free, no model/network, stub Trajectory/Model):
- [ ] `build_env_context` output contains the cwd, an OS token, a shell token, today's `YYYY-MM-DD`
      date (injected `now`), and every granted dir passed in.
- [ ] A huge granted-dir list stays bounded (capped count + `+N more`).
- [ ] `set_env_context` pins the block and it survives a forced compaction.
- [ ] Calling `set_env_context` again REPLACES the block (old marker gone, new present) — per-turn
      refresh, not pin-stale.
- [ ] An oversized block is capped to `MAX_MESSAGE_CHARS` (+ the truncation note) — specs/0009.
- [ ] `config.SITUATIONAL_CONTEXT` defaults `False`.
- [ ] `build_system_prompt(...)` contains NEITHER the date NOR a git branch — dynamic state stays OUT
      of the cached system prompt.

`python scripts/check_situational_git.py` (dep-free, git runner stubbed / tolerant):
- [ ] `_format_git` with a many-file porcelain shows the branch, the total changed count, a file list
      capped to K with a `+N more` marker, and stays under `MAX_MESSAGE_CHARS`.
- [ ] `include_git=False` emits no git line and never invokes the runner.
- [ ] The real `_git_status` against a fresh non-repo temp dir returns `None` and does not raise.
- [ ] A `git_status_fn` that raises is swallowed — no git line, block still returned.
- [ ] The branch line is present when the runner reports a branch.

## Non-goals

- **Static state in `BASE_PROMPT`** — rejected; the block is a per-turn refreshed pin, not a system
  prompt fragment (the harness asserts `build_system_prompt` carries no date/git).
- **A file-watcher / full file-staleness tracker** — a later phase; the per-turn git line covers the
  cheap part only.
- **A trajectory schema bump** — the block rides the existing pin (as-sent view) + a logged `turn`
  (raw view); no new record type.

## Notes

- Proportionality: P2 runs `git status` once per `run()` (per user turn, NOT per model step), behind
  its own default-off flag, with a `shutil.which` guard, a subprocess timeout, a capped file list, and
  a clean degrade to no git line on any error — so a huge/slow worktree can't stall or flood a turn.
- All patterns here are our own Python; no external agent is referenced.

## Files

- **ADD** `src/envcontext.py`, `scripts/check_situational.py`, `scripts/check_situational_git.py`, this spec.
- **UPDATE** `src/context.py` (`pinned_env` + `set_env_context` + `_base`), `src/agent.py` (per-turn
  injection), `src/config.py` (two flags), `.env.example` (document them), `README.md` (repo layout).
- **DELETE** none.
