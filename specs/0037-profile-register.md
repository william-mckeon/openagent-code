# 0037 — `--set-name` registers the launcher in `$PROFILE`

Status: accepted
Builds on: specs/0036 (the `--set-name` / `--remove-name` verbs). Promotes the "opt-in auto-append to
`$PROFILE`" non-goal from 0036 into default behavior.

## Goal

After `--set-name arcus`, typing `arcus` should Just Work in a new shell — without the operator manually
editing `$PROFILE`. In 0036 the verb *printed* the `. "…\scripts\arcus.ps1"` line for a manual paste; a
live session showed the friction (the launcher was generated, but `arcus` was "not recognized" until the
profile line was added by hand). So `--set-name` now **registers** that line in the PowerShell profile
itself, and `--remove-name` **un-registers** it.

## Concepts

### Resolving `$PROFILE` from Python

Python cannot read PowerShell's `$PROFILE` automatic variable, so we ask PowerShell for it:
`<pwsh|powershell> -NoProfile -Command "$PROFILE"` (— `-NoProfile` so resolution never runs the user's own
profile). Both editions are tried: **pwsh** (PowerShell 7) first, then **powershell** (Windows PowerShell
5.1); each installed edition's CurrentUserCurrentHost profile is registered, so the launcher works whichever
the operator uses. On the reference machine that resolves to
`…\OneDrive\Documents\WindowsPowerShell\Microsoft.PowerShell_profile.ps1` (5.1; no PS7).

### Idempotent, reversible line management (pure)

`src/installshim.py` gains two PURE helpers (string → string, unit-tested with no filesystem):

- `profile_ensure(text, line)` → `(new_text, changed)` — append `line` only if not already present
  (exact stripped-line match), preserving every existing line. Idempotent: a second call is a no-op, so
  re-running `--set-name` never duplicates the line.
- `profile_remove(text, line)` → `(new_text, changed)` — drop every occurrence of `line`.

`cli.py` does the thin I/O: resolve the profile path(s), read, apply the pure helper, and write back
atomically (temp + `os.replace`), creating the profile file + its directory if missing.

### Behavior

- `--set-name <name>` — writes `.env`, generates `scripts/<name>.ps1`, and **registers** the dot-source line
  in each resolved profile (idempotent). Prints which profile it touched (`added` / `already present`).
- `--set-name <name> --no-profile` — the old 0036 behavior: generate + print the line, do NOT touch any
  profile (for operators who manage their profile by hand).
- `--remove-name` — reverts `.env` to the OAC default, removes `scripts/<name>.ps1`, and **un-registers**
  the line from each profile.
- Graceful degradation: on POSIX / when no PowerShell is found, registration is skipped and the manual line
  is printed (0036 behavior) — never an error.

## Acceptance (checks in `scripts/check_naming.py`)

1. `profile_ensure` appends the line when absent and preserves existing lines.
2. `profile_ensure` is idempotent — a second apply changes nothing and never duplicates the line.
3. `profile_ensure` handles an empty / newline-terminated profile without a doubled or missing separator.
4. `profile_remove` drops the line (and is a no-op when it is absent), preserving the other lines.

(The subprocess `$PROFILE` resolution and file I/O live in `cli.py` and are exercised manually / by the
grounded machine check, not the dep-free harness — the harness owns the pure, deterministic logic.)

## Non-goals

- A cross-shell launcher beyond PowerShell + the POSIX shell script (a 0036 non-goal, still deferred).
- Editing a machine/all-users profile — only the CurrentUserCurrentHost profile is touched.

## Notes

`--set-name` / `--remove-name` are new verbs (0036), so there is no byte-identity surface to preserve here;
the pure helpers are covered by the harness, and `docker/code/Dockerfile` remains a deliberate non-edit
(headless one-shot, no profile). This changes 0036's print-only step to a real registration; 0036's spec
text is left as historical and this spec is the current behavior.
