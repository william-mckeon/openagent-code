# 0074 — resume integrity (six bug-hunt findings)

Status: implemented
Flag: none new (correctness fixes to `--resume` and the per-task planner reset)

## Goal

Fix the six resume-cluster findings — including the two that can PERMANENTLY break a resumed session — so
`--resume` reliably rehydrates a stopped session instead of poisoning, crashing, or lying to the model about
its state.

## The six fixes

- **A dangling `tool_use` permanently poisoned a resumed session** (`context.py`). `sanitize_tail` only
  repaired the trailing/leading edges, so a MID-list assistant tool_call with no matching result — or a group
  where only SOME of a parallel call's results were logged — survived rehydration, and Bedrock's Converse API
  rejected the unpaired `tool_use` on EVERY later step (and rollback restores the same tail, so it never
  recovers). `sanitize_tail` is now a FULL-history pairing scan: it pairs each assistant tool_call id against
  the following tool results, **synthesizes a stub result** for a missing id MID-history (keeping surrounding
  turns valid) and **drops** a TRAILING incomplete group. The original specs/0034 tail/leading behavior is
  preserved.
- **Stale env blocks re-injected on resume** (`session.py`). The rebuild stripped only a LEADING `role:'system'`
  turn, but with `CODE_SITUATIONAL_CONTEXT` on, `log_env_capture` writes a `role:'system'` env block per turn;
  those stale cwd/date/git blocks were re-sent mid-conversation each step, violating the specs/0035
  single-sent-copy invariant and feeding the model a stale date. `_working_from` now filters ALL `role:'system'`
  turns (the ContextManager owns the real system prompt; the env pin is refreshed per turn).
- **Resume crashed on a truncated last line** (`session.py`). A process killed mid-write leaves a half-written
  final JSON line; the list-comprehension parse raised `JSONDecodeError` and the caller catches only
  `FileNotFoundError`, so a single bad byte lost the whole session. `_load_records` now parses line-by-line and
  SKIPS a corrupt/truncated record.
- **Mid-session directory grants not restored** (`cli.py`/`trajectory.py`/`session.py`). `--resume` rebuilt
  permissions only from flags, so a `/add-dir` or trusted-user-dir grant was gone — yet the rehydrated history
  still told the model it had access, and the weak model retried the now-fence-denied read in a loop. Grants
  are now written as a typed `dir_grant` record (`Trajectory.log_dir_grant`) and replayed on resume by tier
  (`_replay_dir_grants`) — no re-parsing of the model-visible prose.
- **The JSON planner's nudge budget leaked across tasks** (`planner.py`/`agent.py`). The REPL reuses one
  planner; `JsonPlanner.nudges` was reset only on a successful call, so a turn that exhausted its nudges left
  the next unrelated task's first protocol slip going straight to `gave_up` with zero corrective nudges.
  `JsonPlanner.reset()` clears it, called from `agent.run`'s per-task reset (NativePlanner has no reset → no-op).

## Acceptance

`scripts/check_resume_0074.py` (10/10, dep-free — session.py's model imports are now lazy), no regression in
`check_resume` (13/13), `check_situational` (17/17), `check_context` (21/21), `check_repl_outcomes` (30/30):

- `sanitize_tail`: a mid-list dangle is stubbed and later turns survive; a partial-results mid group stubs the
  missing id; a trailing partial group is dropped; the original clean/trailing/leading cases still hold.
- `_working_from` strips all system turns; `_load_records` skips a corrupt final line; `_replay_dir_grants`
  restores by tier and no-ops when there are no grant records; `JsonPlanner.reset()` clears nudges.

## Non-goals

- The dangling-tool_use stub is a placeholder result (`"(interrupted — no result…)"`), not a re-run of the
  tool — resume only needs a VALID pairing, not the lost output.
- The `dir_grant` record is additive (convert ignores unknown types) — no `SCHEMA_VERSION` bump.
- Not a fix for the producers of a dangling tool_use (non-dict tool args, a mid-write crash) — those are their
  own findings; `sanitize_tail` is the resume-side backstop.

## Byte-identity

An already-clean history is a `sanitize_tail` no-op; an old trajectory with no `dir_grant` records replays
nothing; NativePlanner has no `reset`. The lazy imports don't change resume behavior. Verified: full dep-free
suite 57/57.
