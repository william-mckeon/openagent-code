# 0030 - working-dir durability (know where "here" is, and that a granted dir is a READ source)

## Goal
The log review caught a wrong-destination write: after several compactions, when asked to "copy the .env
over", the agent PROPOSED writing it into the granted SOURCE tree (`resume helper\...`) instead of the
established working dir (`messing with OAC\...`); the user had to correct it. Two root causes, both closed
behind one default-off flag (`CODE_WORKDIR_PROMPT`); flag-off is byte-identical.

## Concepts
- **The concrete cwd was never durable.** `build_system_prompt` accepted `granted_dirs` (rendered by ABSOLUTE
  path in the system prompt) but never a cwd, so the only absolute location the model durably held was the
  granted READ dir - exactly where the write defaulted. The one place the real cwd is asserted
  (`envcontext.build_env_context`) is a per-turn block gated on `CODE_SITUATIONAL_CONTEXT`, which is OFF by
  default and, even on, is subject to erosion; the system prompt is NEVER compacted. Fix: thread a `cwd` into
  `build_system_prompt` and pin an absolute WORKING DIRECTORY line in that durable region.
- **Read-source vs write-destination was never distinguished.** The granted-dirs note framed a granted dir as
  a review ROOT with no rule that a COPY/CREATE destination is the workspace, so "copy the .env over" read as
  "operate inside the source". Fix: the durable note (and a clause on the granted-dirs note) states a granted
  dir is a READ SOURCE and a copy/create destination is the workspace unless an explicit path is given.
- **Threading.** `runtime.build_agent(..., cwd=None)` forwards it; `cli` (one-shot + repl), `session.resume`,
  and `subagent.run_subagent` (a child shares the parent cwd) pass it. Flag-off renders nothing, so every
  caller is byte-identical.

## Acceptance
- `src/prompts.py`: `build_system_prompt(..., cwd=None)`; a durable WORKING DIRECTORY note (flag-gated); a
  read-source-vs-write-destination clause appended to the granted-dirs note (flag-gated). Imports `config`.
- `src/runtime.py`: `build_agent(..., cwd=None)` -> `build_system_prompt(cwd=cwd)`.
- `src/cli.py` (both build_agent calls), `src/session.py`, `src/subagent.py`: pass `cwd`.
- `src/config.py` + `.env.example`: `CODE_WORKDIR_PROMPT`, default false.
- `scripts/check_workdir.py` - dep-free, no model / no network.
- **Flag OFF is byte-identical**: no WORKING DIRECTORY line, the granted-dirs note keeps its old text, and
  `build_system_prompt` with `cwd=None` (or the flag off) is unchanged.

## Traps (each is a test)
- **Durable, not per-turn.** The cwd line lives in the system prompt (never compacted), NOT the situational
  env block - that's the whole point (it must survive compaction, which the incident showed the env block
  did not, being off by default).
- **Flag-gated both ways.** `cwd` passed but flag off -> no line; flag on but `cwd=None` -> no line.
- **Granted-dirs clause is additive.** The old "Reference directories you may READ" text is unchanged when
  the flag is off; the destination clause is only appended when on.
- **A child inherits the cwd.** `run_subagent` passes `parent_ctx.cwd`, so a spawned worker gets the same
  durable pin.

## Non-goals (v1 - deferred, documented)
- **Enforcing granted dirs as read-only in the permission fence** (`permissions._within_roots` / the
  run_command sandbox). The fence pools cwd + `extra_roots` and allows writes to any - but a granted dir is
  ALSO sometimes a legitimate WRITE target (the same log's `request_dir` granted a dir specifically to copy
  files INTO it). Enforcing read-only needs a PER-GRANT read/write distinction (a new field on the grant, at
  `--add-dir` / `request_dir` / `CODE_ADD_DIRS`), a larger and riskier change; this phase fixes the BEHAVIOR
  (the prompt) and leaves enforcement to a follow-up so a legitimate write-grant isn't broken.
- Annotating the granted dirs in the situational env block (that block is already opt-in and off by default;
  the durable prompt is the always-relevant location).
- Auto-detecting the "work only from X" instruction as a hard cwd override (the durable pin + the destination
  rule address the observed slip; a hard override is a separate mechanism).
