# 0033 - safety-config fingerprint (record which guards were on, so a clean run isn't ambiguous)

## Goal
A reviewer flagged a real provenance gap: the run artifact records per-call permission DECISIONS (allow /
ask / deny + the rule/mode that decided), so a guardian DENIAL is traceable - but it does not record the
safety CONFIG itself. So a clean guardian-ON run is byte-indistinguishable from a guardian-OFF run: nothing
says whether `CODE_GUARDIAN` (or the sandbox, or any verify gate) was even active. For the training flywheel
that means "the safety layer ran and passed" can be confused with "the safety layer was absent." Close it by
stamping a human-readable safety-config FINGERPRINT into every run's `session_start`.

## Concepts
- **The fingerprint** (`config.safety_fingerprint(perms) -> dict`) - a readable dict of flag -> value (NOT an
  opaque hash, so it directly answers "which guards were on") covering the four families:
  permission/fence (mode, deny/ask/allow rule counts, fence width), the guards (guardian +
  max-destructive, sandbox, execpolicy, hooks), the declared-done verify family (specs/0032:
  completion / grounding(+semantic/paths) / touched / manifest / mutation_claims), plan+spec discipline
  (propose / spec_first / goal_loop), the effort policy, and the external-reach boundary (enable_web /
  mcp_web_active / web_grounding_active / workdir_prompt). Reads EXISTING flags only - no new `CODE_*` var.
- **Why it takes `perms`.** `Permissions.from_config` resolves `--mode` / `--add-dir` ONTO the Permissions
  object and never writes back to `config`, so a `--mode acceptEdits` run leaves `config.resolved_permission_mode()`
  showing `bypass`. The `permission_mode` and `extra_roots` (fence width) therefore come from `perms`;
  everything else from module globals + the rule counts.
- **Captured at session_start TIME.** The fingerprint is computed at the Trajectory construction sites (which
  hold the Permissions) and passed in as `safety=`, so it observes the runtime-mutated flags too
  (`config.PROPOSE` set in `cli.main` for propose mode; `config.MCP_WEB_ACTIVE` set by `mcp_client.connect`) -
  both run BEFORE any Trajectory is built.
- **Where it lands.** `Trajectory.__init__` gains a `safety=None` param and writes `session_start.safety` ONLY
  when supplied - so legacy / test constructions stay byte-identical and old trajectories convert unchanged.
  All corpus-writing sites pass it: cli one-shot + repl, subagent (children write to the same corpus), and
  BOTH eval/harness paths (train/capture routes the flywheel through them).

## Acceptance
- `src/config.py`: `safety_fingerprint(perms=None)` (perms -> mode + fence width; globals + rule counts for
  the rest). `src/trajectory.py`: `__init__(..., safety=None)`; `session_start` gains `safety` when supplied
  (omitted when None); `SCHEMA_VERSION` 0.12.0 -> 0.13.0 + a changelog line.
- Call sites pass `safety=config.safety_fingerprint(perms)`: `src/cli.py` (_one_shot + _repl),
  `src/subagent.py` (run_subagent, from the parent's perms), `eval/harness.py` (run_task + run_agentic_task,
  with `perms` hoisted above the Trajectory).
- `scripts/check_completion_honesty.py`: the `SCHEMA_VERSION` assertion + fixture -> 0.13.0.
- `scripts/check_config_provenance.py` - dep-free: the fingerprint records the guards; guardian-ON vs
  guardian-OFF DIFFER; `permission_mode` tracks `perms.mode` under a `--mode` override (not the config global);
  `session_start` records it when supplied and OMITS it when None; a legacy record (no `safety`) still converts.
- `docs/DATASHEET.md`: the `safety` field noted on the `session_start` row.
- **Byte-identical**: behavior is unchanged (only capture is richer, not a flag-gated branch); a construction
  without `safety=` (legacy/test) writes no field; `outcomes.py`/`convert.py`/`.env.example` need no change.

## Traps (each is a test)
- **`perms.mode`, not the config global.** A `--mode acceptEdits` run must record `acceptEdits`, though
  `config.resolved_permission_mode()` still says `bypass`.
- **Guardian must be IN the dict.** The whole payoff (invariant c) holds ONLY if `guardian` is recorded - a
  bypass/read-only run never fires the guardian, so without the flag the two runs are identical.
- **Omit when None.** No `safety` key on a legacy/test `session_start`, so old data + golden fixtures convert.
- **Append the param.** `safety=None` goes AFTER `depth` so every positional/keyword caller stays valid.
- **Every corpus path.** cli, subagent, AND both eval/harness paths (capture's corpus) - miss one and those
  rows stay unlabeled.

## Non-goals (v1) - the follow-up we agreed to design AFTER
- **Re-stamping on state change.** This is a LAUNCH-time snapshot. A `--resume` (resume() doesn't re-emit
  `session_start`), or a mid-session REPL `/mode` / `/add-dir` (which mutate `ctx.permissions` live), does NOT
  re-fingerprint. A follow-up should stamp a config-drift fingerprint onto the `session_resume` record and on
  a live mode/fence change, so the provenance tracks the *effective* config over the whole session, not just
  its birth. Deliberately out of scope here to keep the fix minimal and byte-identical.
- **An opaque digest** for fast cross-run grouping (the readable dict closes the gap; a short hash could layer
  on later).
- **convert.py surfacing** the fingerprint in report.json (pure auditability; the field is already tolerated).
