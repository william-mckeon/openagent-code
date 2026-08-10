# 0072 — four bugs the log review surfaced

Status: implemented
Flag: none new (correctness fixes; N4 rides the existing `CODE_SHELL_HINTS`)

## Goal

Fix the four NOVEL bugs a full review of `logs/*.log` found — issues real runs exhibited that the 37-bug hunt
had not catalogued. (The review also re-confirmed 18 already-fixed behaviors and mapped 0 to the 0072-family
backlog.)

## The four fixes

- **N1 — a missing required tool arg leaked a raw `KeyError`** (`tools.py`). `edit_file`/`write_file`/
  `delete_file` do `args["path"]` unchecked; `Registry.run`'s catch-all surfaced the raised `KeyError` as
  `Tool error: KeyError: 'path'` — a cryptic Python error the model couldn't self-correct from. Fix:
  `Registry.run` validates the tool schema's `required` args before dispatch and returns
  `missing required argument: 'path'`. It also treats a non-dict `args` (a JSON-valid string/None argument) as
  no args, so that case degrades to the same clean message instead of crashing downstream.
- **N2 — a subagent under propose mode was permanently deadlocked** (`permissions.py`). A depth>0 child
  inherits `mode='propose'` but `propose_changes`/manifest approval are top-level-only, so it could NEITHER
  mutate NOR approve — and the deny text ("read-only until the manifest is approved") implied approval was
  possible, driving ~22 retry steps in one run. Fix: `Permissions._propose_ro_msg(ctx)` is depth-aware — at
  depth>0 it returns a terminal, honest deny ("a SUBAGENT cannot approve a change-list … RETURN your findings
  to the top-level agent, do NOT retry"). All three propose read-only deny sites route through it.
- **N3 — Windows `run_command` output mojibake** (`tools.py`). Output was decoded as UTF-8 but PowerShell 5.1
  emits cp1252/OEM, so em-dashes/arrows came back as `�?"` and the agent false-diagnosed "copy corruption".
  Fix: `_shell_invocation` prepends `_PS_UTF8_PRELUDE` (`$OutputEncoding = [Console]::OutputEncoding =
  [Text.UTF8Encoding]::new($false);`) on nt, so console output is emitted as UTF-8 and matches the decode.
- **N4 — `2>&1` on a native exe flipped the exit code** (`prompts.py`/`envcontext.py`). PowerShell wraps a
  native exe's stderr as a `NativeCommandError` and returns non-zero even on success, so `docker logs … 2>&1`
  with valid `HTTP 200` output was logged `[FAIL]`. Fix: a `CODE_SHELL_HINTS` clause tells the model not to
  `2>&1` a native exe (docker/git/curl/npm) — let stderr flow (it's captured) or use `-ErrorAction`.

## Acceptance

`scripts/check_logfix_0072.py` (9/9, dep-free), plus updated `check_shell_noninteractive` (5/5, now expects the
prelude), no regression in `check_permissions`/`check_propose`/`check_situational`:

- N1: a missing required arg → clean "missing required argument: 'path'" (no `KeyError`); a valid call still
  dispatches; a non-dict args value degrades safely; a no-required-args tool is unchanged.
- N2: depth 0 keeps the original text; depth>0 gets the terminal "cannot approve / report up / do NOT retry".
- N3: the prelude sets a no-BOM UTF-8 `OutputEncoding` and is prepended before the real command (nt);
  posix bash is unchanged.
- N4: the 2>&1 hint renders under `CODE_SHELL_HINTS` on nt, and never with hints off (byte-identical).

## Non-goals

- N3 is applied unconditionally on nt (not flag-gated): it makes PowerShell output MATCH the pre-existing
  utf-8 decode — fixing an inconsistency, not adding a feature.
- N4 is a prompt hint (mitigation); it does not change how `run_command` computes success. A deeper
  exit-code-from-native-stderr fix is out of scope.
- N2 fixes the deny MESSAGE (honest + actionable); it does not change subagent mode inheritance (a larger
  follow-up).

## Byte-identity

N1 only changes the result for a call that was already going to fail (missing/!dict args). N2 only changes the
deny text at depth>0. N3 is nt-only and posix is untouched. N4 rides `CODE_SHELL_HINTS` (off → byte-identical).
Verified: full dep-free suite 55/55.
