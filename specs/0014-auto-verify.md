# 0014 — Auto-verify (lint/test/compile on touched files + reflection loop)

> After the completion gate proves the agent's file changes are REAL, run a configured check
> (default `python -m py_compile`) on just the TOUCHED files, feed any errors back as a bounded
> reflection turn, and record pass/fail as an OBJECTIVE reward label. Phase 14 of the adoption track.
> All patterns are our own Python; no external agent is referenced.

## Why

Verified completion (specs/0007) proves the agent DID the work; grounding (specs/0010) proves its CLAIMS
are backed. Neither proves the change actually WORKS. A weak model ships an edit that doesn't compile and
declares victory. Auto-verify is the third check: run an objective command on the changed files and, if
it fails, hand the errors back so the model self-corrects until clean.

This is THE flywheel multiplier. The harness already records test/lint pass/fail as a reward signal, and
the failed → read-error → fixed reflection turns are premium training data — this closes the exact loop
the flywheel was built to feed.

## Safety — why the harness-run command is safe even before OS sandboxing (specs/0017)

The concern with running a command the harness initiates is that it bypasses the model-facing permission
gate. This design removes the risk at the source rather than deferring it:

- **argv lists, never a shell string.** A verifier is a list — default `["python", "-m", "py_compile",
  "{file}"]` — run with `shell=False`. There is NO shell to inject into, so a hostile filename cannot
  smuggle a command.
- **operator-configured, never model-controlled.** The command comes only from the safe built-in default
  merged with `CODE_VERIFY_CMDS_CONFIG` (a JSON map of ext → argv list). The model cannot choose it.
- **the only variable is the touched-file PATH**, taken from `ctx.mutations` (already workspace-fenced),
  substituted into the argv.
- **timeout + workspace cwd + fail-OPEN.** A run error / unconfigured extension yields NO problem, so an
  infra hiccup never traps the agent (the completion gate already guaranteed the work was done).

OS-level confinement of the operator's own configured command is added by specs/0017 as defense-in-depth.

## Sub-phases (each independently shippable, behind its flag)

- **A — verifier module** (`CODE_VERIFY_TOUCHED`): `src/verify_edits.py`, mirroring `grounding.py` — pure
  functions + an injected `run_fn`. `verifier_cmds()` (ext → argv, py_compile default merged with the
  optional JSON config), `select(touched)`, `run_checks(selected, run_fn)`, `parse_errors`, `challenge`,
  `problems(ctx, run_fn)`. Acceptance: `scripts/check_verify_edits.py`.
- **B — the third gate**: in `src/agent.py`'s no-tool-call branch, AFTER the completion gate accepts and
  BEFORE the grounding gate, run `verify_edits.problems(ctx)`; a failure re-prompts (bounded by
  `CODE_VERIFY_TOUCHED_RETRIES`), then records an honest `verify_failed_edits` outcome. Mirror the outcome
  at `cli.py`, `subagent._classify`, `eval/harness.py`, `eval/rubric.py`. Acceptance:
  `scripts/check_verify_gate.py`.
- **C — reward label** (`CODE_VERIFY_TOUCHED_LABEL`): emit one `trajectory.log_verification` record per
  touched-file check (reuse the existing objective-reward record — no schema type added). Acceptance:
  `scripts/check_verify_reward.py`.

## Acceptance (checkable)

- [ ] `scripts/check_verify_edits.py` (dep-free, stub `run_fn`): `select` picks `.py` writes/edits and
      skips deletes / non-`.py` / no-verifier; a FAIL surfaces a structured problem; a raising `run_fn`
      fails OPEN; `challenge` names the file and stays non-hijacking; a custom config merges; the flag
      defaults off.
- [ ] `scripts/check_verify_gate.py` (dep-free): the third gate composes with completion + grounding
      (own bounded counter), re-prompts on failure, returns `verify_failed_edits` on exhaust, and with
      the flag OFF the branch is byte-identical to today.
- [ ] `scripts/check_verify_reward.py` (dep-free): a reward record is emitted per touched-file check; the
      rubric caps a `verify_failed_edits` run; `convert.KEEP_OUTCOMES` drops it (but keeps a
      failed-then-fixed run that ends `completed`/`success`).

## Non-goals

- **Whole-repo test runs** — scope is TOUCHED files only (proportionality; never a repo-wide suite).
- **A shell-string command format** — argv lists only (the injection-free design above).
- **A new trajectory record type** — reuse `log_verification`.

## Notes

- Proportionality (the recurring lesson): default OFF, touched-files only, safe `py_compile` default,
  fail-OPEN, bounded retries → honest outcome, non-hijacking challenge.
- `train/convert.py` needs **no change**: `KEEP_OUTCOMES = {"success","completed"}` auto-drops
  `verify_failed_edits`, and a run that failed then FIXED it ends `completed`/`success` and is KEPT — only
  genuinely-unfixed failures drop.
- `eval/harness.py` correction: the function is `run_task` and it does not yet honor the honest gate
  outcomes, so honoring `verify_failed_edits` there is NEW mapping, not a mirror of existing code.

## Files

- **ADD** `src/verify_edits.py` (A), `specs/0014-auto-verify.md`, `scripts/check_verify_edits.py` (A),
  `scripts/check_verify_gate.py` (B), `scripts/check_verify_reward.py` (C).
- **UPDATE** `src/agent.py` (the third gate), `src/config.py` (the flags), `src/cli.py` +
  `src/subagent.py` (the `verify_failed_edits` outcome), `src/trajectory.py` (the reward record),
  `eval/rubric.py` (the cap), `eval/harness.py` (honor the outcome), `.env.example`, `README.md`.
- **NO CHANGE (confirmed):** `train/convert.py` (`KEEP_OUTCOMES` auto-drops the failure label).
- **DELETE** none.
