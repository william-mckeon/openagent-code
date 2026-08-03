# 0055 — non-interactive shell (no REPL hang on stdin)

Status: implemented
Flag: `CODE_SHELL_NONINTERACTIVE` (default off)

## Goal

Stop a `run_command` from hanging the REPL waiting for input the agent never sends. `run_command` is meant
for one-shot commands, but its child inherits the parent's stdin, so a command that READS stdin blocks
forever. A live Centpilot run hit this hard: the model emitted a bare PowerShell `echo` as a blank-line
separator (a bash idiom), and in PowerShell `echo` is `Write-Output` — with no argument it PROMPTS
("Supply values for the following parameters: InputObject[0]:") and reads the console. The whole session
froze, and the user had to type garbage ("whaat what what") into the hung prompt to escape. `Read-Host`,
`Get-Credential`, or a foreground server started inline would hang the same way.

## Concepts

- **Fail fast instead of blocking on stdin.** When `CODE_SHELL_NONINTERACTIVE` is on, `_shell_invocation`
  builds the subprocess with the child's **stdin = `DEVNULL`** (both platforms) and PowerShell gets
  **`-NonInteractive`**. Any stdin read then returns EOF / errors immediately rather than waiting for a human,
  so a bare `echo`, `Read-Host`, or a prompt can never freeze the REPL. Paired with the specs/0051 shell-hint
  (extended here) that tells the model not to emit a bare `echo` in the first place.
- **A pure, testable builder.** The argv + stdin choice moved into `tools._shell_invocation(cmd)` so it can
  be asserted dep-free (no subprocess). `run_command` calls it and passes `stdin=` through to
  `subprocess.run`.
- **Byte-identical when off / for normal commands.** OFF: the prior argv (`powershell -NoProfile -Command`)
  and inherited stdin (`None`) — unchanged. ON: the only behavioral difference is that a command which reads
  stdin now gets EOF instead of hanging; a command that does NOT read stdin (all of them, in practice)
  produces identical output, because DEVNULL vs inherited stdin is invisible to a process that never reads it.

## Acceptance

Each item is an assertion in `scripts/check_shell_noninteractive.py` (5/5), plus the extended shell-hint
assertion in `scripts/check_situational.py`.

- `_shell_invocation` OFF: `['powershell','-NoProfile','-Command',cmd]` (or `bash -lc`) with stdin `None`.
- `_shell_invocation` ON: PowerShell gains `-NonInteractive`; stdin is `subprocess.DEVNULL`.
- ON, real subprocess: a normal command (`Write-Output hi`) still runs and returns its output.
- ON, real subprocess: a stdin-reading command (`[Console]::In.ReadToEnd()` / `cat`) RETURNS instead of
  hanging (DEVNULL gives it EOF).
- `CODE_SHELL_NONINTERACTIVE` defaults False when unset.
- Shell hint (specs/0051 block, `CODE_SHELL_HINTS`): "NEVER run a bare `echo` / `Write-Output` with no
  argument (it PROMPTS for input and HANGS)".

## Non-goals

- Not a timeout change — `run_command` still caps at 120s (a genuinely long non-interactive command is a
  different concern). This only removes the INPUT-wait hang.
- Not `-NoProfile` toggling (that is already always on) and not a general PowerShell-vs-bash rework.
- No `SCHEMA_VERSION` bump; the flag is not in `safety_fingerprint`.

## Byte-identity

`CODE_SHELL_NONINTERACTIVE` off (default): `_shell_invocation` returns the exact prior argv and `stdin=None`,
so `run_command` is byte-for-byte what it was. Verified: the OFF assertions in
`check_shell_noninteractive.py`; the rest of the suite unchanged.
