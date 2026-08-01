# 0050 — self-preservation

Status: implemented
Flag: `CODE_GUARD_SELF_KILL` (default off)

## Goal

Stop the agent from killing its own process. Arcus runs as `python -m src`, so a NAME-based process kill
terminates the very interpreter it is running in. A live Centpilot run did exactly this: verifying the app,
the model tried to spin up a test `http.server` and clean it up with `Stop-Process -Name python` — which
matched Arcus itself and ended the REPL mid-task, with no clean shutdown (the run log simply stopped after
the last command, no error, no `/exit`). Nothing prevented it: `execpolicy` flags `kill -9` / `pkill -9`
but not `Stop-Process` / `taskkill` / a name-based `pkill python`, and the run was in `bypass` mode, where
even a "dangerous"-classified command is auto-allowed.

## Concepts

- **A hard deny that outranks the mode.** `_decide_core` gains a first check: when `CODE_GUARD_SELF_KILL` is
  on and a `run_command` matches `_is_self_kill`, it is DENIED in EVERY mode — including `bypass` — because
  it runs before the execpolicy routing and the mode ladder, the same way a deny rule and the workspace
  fence win regardless of mode. Killing yourself is never a legitimate agent action.
- **Name-based only; PID-kill is untouched.** `_is_self_kill` matches a kill verb (`Stop-Process`,
  `taskkill`, `pkill`, `killall`, `kill`) followed by `python` WITHIN one command segment — a `;`/`&&`/`||`/`|`
  boundary stops the match, so `Stop-Process -Name foo; python x` is not flagged. `Stop-Process -Id <n>` and
  `taskkill /PID <n>` carry no `python` token and stay allowed, so the legitimate way to stop a spawned test
  server (kill its PID) still works. The paired shell-hint (specs/0046) tells the model to do exactly that.
- **Blunt on purpose.** A name-based python kill *is* self-destructive on this deployment; denying it is the
  correct outcome even when the model "meant" to kill a child server — it should target the PID instead.

## Acceptance

Each item is an assertion in `scripts/check_self_preservation.py` (6/6, dep-free, no model).

- `_is_self_kill` flags every name-based python kill (`Stop-Process -Name python[.exe]`, `taskkill /F /IM
  python.exe`, `pkill python`, `killall python`, `kill -9 python`, `a; Stop-Process -Name python`).
- `_is_self_kill` does NOT flag kill-by-PID, bare `python …`, a segment-separated python, or a read command.
- Flag ON: a name-based self-kill is DENIED in every mode — bypass, default, acceptEdits, plan, propose.
- Flag ON: kill-by-PID is not blocked by the guard (bypass allows it).
- Flag OFF: the self-kill is not blocked (byte-identical — bypass allows it as before).
- `CODE_GUARD_SELF_KILL` defaults False when unset.

## Non-goals

- No PID-based self-kill detection — matching the agent's own `os.getpid()` inside a command is more
  fragile, and a model rarely knows its own PID; the name-based blunt kill is the observed and dominant
  failure. Left as recorded debt.
- Not an execpolicy classification change — the guard is a dedicated deny in `permissions.py`, so
  `check_execpolicy` stays byte-identical.

## Byte-identity

`CODE_GUARD_SELF_KILL` off (default) short-circuits the guard before `_is_self_kill` is ever called, so the
permission ladder is byte-for-byte what it was before specs/0050 (verified: `check_permissions` 36/36,
`check_execpolicy` 44/44 unchanged; the flag-off assertion in `check_self_preservation.py`). No
`SCHEMA_VERSION` bump; the flag is not in `safety_fingerprint`.
