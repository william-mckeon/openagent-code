# 0076 — fan-out & robustness (six bug-hunt findings)

Status: implemented
Flag: none new (correctness fixes across the fan-out / hook / tool layer)

## Goal

Fix the fan-out cluster — the last non-harness batch of the bug hunt: a hook timeout that didn't work on
Windows, a review-coverage false-positive, a silent workflow phase-drop, late subagent error-containment, a
grep false-absence, and a narration-guard multiline edge.

## The six fixes

- **The hook timeout was unenforceable on Windows** (`hooks.py`). `subprocess.run(shell=True, timeout=…)` on
  Windows kills only the intermediary shell (`cmd.exe`), leaving the real hook (a grandchild) running — and
  `communicate()` then BLOCKS on its still-open pipe, defeating the timeout. Now `Popen` with a process GROUP
  (`CREATE_NEW_PROCESS_GROUP` / `start_new_session`) + a `_kill_tree` (taskkill `/T` on Windows, `killpg` on
  POSIX) on timeout, so a hung hook is actually terminated and the run fails open in bounded time.
- **A review-coverage false positive** (`orchestrator.py`). `_balance_plan` checked whether each folder was
  covered with a SUBSTRING test against a space-joined blob of area labels, so a folder whose name was a
  substring of any label (`app` inside `mapper/`) was silently treated as covered and never got its own area.
  Now the check is exact against the FIRST path component of each area (a set).
- **Over-cap workflow phases dropped silently** (`workflow.py`). Phases beyond `CODE_MAX_WORKFLOW_PHASES` were
  dropped with only a verbose console line; the digest the model/user reads said nothing. The final digest now
  carries a `[NOTE] N phase(s) … were NOT run: …` line (mirroring the per-job truncation note).
- **Subagent error-containment started too late** (`subagent.py`). The `try` began after `Trajectory()` /
  `make_context()` / `build_agent()`, so a failure in CHILD CONSTRUCTION escaped `run_subagent` and could crash
  the whole fan-out. The `try` now wraps construction too, returning `(subagent error: …)` (guarding
  `traj.end` for the case where `Trajectory()` itself raised).
- **`grep` on a FILE path returned a false absence** (`tools.py`). `os.walk` on a file yields nothing, so
  grepping a file that DID contain the pattern returned `(no matches)`. A file `path` is now searched directly.
- **The narration guard misread a multiline command** (`agent.py`). `_NARRATION_META` lacked `\n`, so a
  command whose first line was a quoted `Write-Output` but whose LATER lines did real work classified as pure
  narration. A newline OUTSIDE quotes (a second statement) now disqualifies it — a multiline *string* print
  still counts (its newlines are inside the quoted span).

## Acceptance

`scripts/check_fanout_0076.py` (7/7, dep-free via fake litellm + a real 1s hook), no regression in
`check_hooks` (31/31), `check_fanout` (17/17), `check_workflows` (22/22), `check_search` (28/28),
`check_narration_stall` (9/9):

- a hung hook (sleep 30, timeout 1) fails open within seconds; a folder that's a substring of an area still
  gets its own area; `plan_phases` returns the over-cap phases as `dropped`; a construction error is contained;
  `grep` on a file finds/absents correctly; a multiline Write-Output-then-real-work command is not narration.

## Non-goals

- `_kill_tree` is best-effort (falls back to `proc.kill()`); the hook path is opt-in + fail-open by design.
- Not a change to the fan-out cap values or the review partitioning strategy — only the coverage CHECK.

## Byte-identity

Each fix only changes behavior for the mishandled input (a hung hook, a substring-collision folder, an over-cap
phase, a construction error, a file-path grep, a multiline command). Verified: full dep-free suite 59/59.
