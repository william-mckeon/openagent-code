# 0025 — spec-first (the agent authors a design+acceptance spec before it builds)

## Goal
Give openagent-code the discipline the maintainers use on it: for a substantive change, **author a
persistent design+acceptance spec first**, get it approved, implement against it, and don't report "done"
until the acceptance items are met. It's the third artifact beside two that already exist but aren't this —
`update_plan` (ephemeral task steps) and `propose_changes` (an ephemeral change-LIST). A spec is the
durable **contract**: Goal / Acceptance (a checklist that defines done) / Non-goals.

## Concepts
- **The artifact** — a persistent markdown spec at `<workspace>/.openagent/specs/NNNN-slug.md` (auto-
  numbered, co-located with memory.md and todos.md; the whole `.openagent/` tree is already gitignored).
  Sections **Goal / Acceptance / Non-goals**, the same shape as this repo's own `specs/` folder. Kept
  DISTINCT from the maintainers' `specs/` — the agent's specs live per-repo under `.openagent/specs/`. The
  ACTIVE spec is the highest-numbered file; it's loaded into the system prompt and reloaded on resume, so
  the agent builds against it (mirroring the memory / project-todos plumbing exactly).
- **The store** (`src/specstore.py`) — the memory/todos store analog, but a NUMBERED directory of specs and
  a BINARY acceptance checklist (`- [ ]` / `- [x]`, not todos' tri-state). `path`/`specs_dir`/`next_number`
  /`slugify`/`parse`/`render`/`save`/`load_active`/`active_text`/`set_acceptance`/`outstanding`/`all_met`.
  `parse(render(spec))` round-trips; `load_active` never raises (missing/malformed dir → None). `save`
  mints the number ONCE with a collision guard; re-save is idempotent to the same file.
- **The tool** (`write_spec`) — a registration tool like `propose_changes`. `action='propose'` (default):
  validate `{title, goal, acceptance[], non_goals[]}`, persist the file FIRST, stash `ctx.spec`, and collect
  ONE approval (`ctx.ask`) — top-level only; headless writes the draft and STOPS, never auto-approves.
  `action='done'`: mark an acceptance item met (by its number or exact text) and rewrite the file, so the
  agent checks items off as it satisfies them. Non-mutating (stays OUT of `permissions.MUTATING` — like
  `remember`/`update_plan`/`propose_changes` — so it works in plan / read-only phases).
- **The acceptance gate** (`src/agent.py`) — the teeth. When the agent declares done and an APPROVED spec
  is active (`config.SPEC_FIRST and ctx.spec and ctx.spec['approved']`), any acceptance item not marked done
  re-prompts (bounded by `CODE_SPEC_FIRST_RETRIES`), then records an honest `acceptance_unmet`. The check is
  **mark-based** — the deterministic mirror of the completion gate (specs/0007), no model, no NL matching of
  the answer. It sits after the goal gate and before grounding; `ctx.spec` is reset every task so an
  approved-but-unfinished spec can't hijack an unrelated later turn.
- **Config** — `CODE_SPEC_FIRST` (default false), `CODE_SPECS_DIR` (`.openagent/specs`), `CODE_SPECS_MAX_CHARS`
  (prompt-injection bound), `CODE_SPEC_FIRST_RETRIES` (2), `specs_dir(workspace)` (workspace-relative, like
  `memory_file`/`todos_file` — NOT install-root like `skills_dir`).

## Acceptance
- `src/specstore.py`: `parse`/`render` round-trip; `next_number` auto-numbers; `save` mints a number once
  with a collision guard + `newline=''`; `load_active` = highest-numbered, never raises; `set_acceptance`
  flips a binary item; `all_met`/`outstanding`; `active_text` whole-line-bounded.
- `src/tools.py`: `write_spec` (propose = validate/persist/stash/approve, top-level-only, headless-stops;
  done = mark + rewrite) + `SPEC_TOOLS` + `Context.spec`. `src/toolset.py`: gated on `config.SPEC_FIRST`.
- `src/agent.py`: `_unmet_acceptance` + `_acceptance_challenge`; the gate (triple-gated, bounded,
  `acceptance_unmet` on exhaustion); reset `ctx.spec`; log the spec once in `_finish`.
- `src/prompts.py`: a `spec=None` param + a `## Active spec` injection (non-empty-string guard) + a
  tool-presence-gated spec-first note. `runtime`/`session`/`cli`: thread `spec=`, load + show at startup,
  reload on resume.
- `src/config.py`: the flags + `specs_dir()`. `src/trajectory.py`: `log_spec` + `SCHEMA_VERSION` 0.11.0.
- `src/outcomes.py`: `spec_declined` + `acceptance_unmet` in `GATE_OUTCOMES`. `train/convert.py`:
  `_unmet_spec_turns` drops a declined/unmet turn (keeps the good ones beside it) + one-shot guard + counter.
- `scripts/check_specs.py` — dep-free, no model/network.
- **Flag OFF is byte-identical**: `write_spec` isn't offered, nothing loaded/injected/printed, every new
  gate branch is skipped, and a spec-less run logs no `spec` record.

## Traps (each is a test)
- **Distinct from the maintainers' `specs/`** — the agent's specs live under `.openagent/specs/`;
  `specs_dir` resolves against the WORKSPACE (per-repo), not `INSTALL_ROOT` like `skills_dir`.
- **Binary acceptance, not tri-state** — `specstore` owns its own `_MARKS`; don't reuse `todos._MARKS`. Parse
  is lenient (`[X]`/`[~]` tolerated; only `[x]`/`[X]` count as met) so a hand-edited spec still loads.
- **Number/slug collision** — mint the number ONCE inside `save()` with a `while os.path.exists: number+=1`
  guard; treat the number as identity, the slug as derived-once.
- **Which spec is active** — the highest-numbered file (no separate pointer to desync).
- **Non-mutating** — `write_spec` writes via `specstore` directly, never `write_file`/`_record_mutation`, and
  stays out of `MUTATING`, or it's blocked in plan mode and the completion gate treats the spec as code.
- **Persist before approving** — the draft survives a decline as a reviewable file.
- **Gate hijack** — the acceptance gate keys on `ctx.spec` (reset per task), NOT the on-disk file existing,
  or every turn in a repo that has a spec fires the gate. Triple-gated + bounded + `config.SPEC_FIRST`-gated
  so flag-off is byte-identical.
- **Mark-based, not NL-matched** — the gate checks marked items, never keyword-matches the answer (brittle;
  the specs/0007 non-goal).
- **Corpus** — a declined/unmet spec drops that turn via a `spec`-record helper (NOT `_contested_turns` — a
  decline writes no permission record and may still end 'completed').

## Non-goals (v1)
- Auto-generating a `check_*.py` harness from the acceptance items (the maintainers hand-write those; a
  later phase could).
- Cross-checking an acceptance item against `ctx.mutations` by a named file (the completion gate already
  does file-backing for `update_plan`; v1 acceptance is mark-based discipline).
- Re-titling a spec after approval (number = identity, slug = derived once).
- A separate "active spec" pointer (highest-numbered wins).

## Notes
- Off by default (`CODE_SPEC_FIRST=false`), like every adoption-track phase; documented in `.env.example`.
- The natural stack: `write_spec` (the contract) → optionally `propose_changes` (the file list) →
  implement → mark each acceptance item → the gate holds "done" until they're all met.
