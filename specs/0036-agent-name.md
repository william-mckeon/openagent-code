# 0036 — A nameable agent (OAC by default; rename at install)

Status: accepted
Config: `CODE_AGENT_NAME` (default **`OAC`**), `CODE_AGENT_PERSONA` (default empty).
Install verbs: `openagent-code --set-name <name> [--persona "…"]` and `openagent-code --remove-name`.

## Goal

The operator can give the coding agent a name of their choosing at install time — the agent answers to
that name, and they can launch it by typing that name — **without renaming the package or any internals**.

- **Default** (fresh install, and after `--remove-name`): the agent is **OAC**.
- `--set-name arcus` → the agent introduces itself as `arcus`, and `arcus` becomes a launch command.
- `--remove-name` → reverts the name *and* persona to the OAC default and removes the generated launcher.

The package/import name stays `openagent-code` (pyproject, `python -m src`, the `openagent-code` console
script are all unchanged). Only the *user-facing identity* — the system-prompt name line and the two
banners — and an *added launcher* change.

## Concepts

### The label (`CODE_AGENT_NAME`, default `OAC`)

`config.agent_name()` is the single source of truth, read by `prompts.build_system_prompt` (it substitutes
the name into the one identity line in `BASE_PROMPT` — `"You are openagent-code,"`, a unique token) and by
both `cli.py` banners. A blank/whitespace `CODE_AGENT_NAME=` coalesces back to `OAC` so the prompt can never
emit `"You are ,"` (**trap B**).

Read once at import — a per-install constant, so the server-cached system prompt is stable within a session
(no per-turn churn). This is the ONE deliberate departure from strict byte-identity-with-today: the default
identity moves from the internal literal `openagent-code` to **OAC** (the operator asked for it); everything
else in the prompt is unchanged.

### The persona (`CODE_AGENT_PERSONA`, default empty)

`config.agent_persona()` returns a single-line, newline-stripped, length-capped (`PERSONA_MAX = 280`) string,
appended to the system prompt **only when non-empty**, with the `\n\n` separator INSIDE the gate so an empty
persona appends literally nothing (**trap A**). Operator-set at install (trusted), but sanitized on both
write and read (**trap C**: a persona with embedded newlines collapses to one line).

### The launcher (`--set-name` / `--remove-name`)

`oac` on the reference machine is a PowerShell `$PROFILE` **function** ([scripts/oac.ps1](../scripts/oac.ps1)),
not a file on PATH — so a Scripts-dir `.cmd`/`.exe` would not be typeable. The launcher therefore **mirrors
`oac.ps1`**: `--set-name arcus` generates `scripts/arcus.ps1` (a PowerShell `arcus` function that calls the
same `openagent-code.exe @args` — which also forwards shell metacharacters losslessly, unlike a `.cmd`),
writes `.env`, and **prints** the exact `. "…\scripts\arcus.ps1"` line to add to `$PROFILE` (mirroring how
`oac` was installed). `--remove-name` deletes the generated `scripts/<name>.ps1`, strips the two vars from
`.env`, and prints the `$PROFILE` line to remove.

The generated `scripts/<name>.ps1` is **gitignored** (`scripts/*.ps1` with `!scripts/oac.ps1`): it is
per-user and bakes in an absolute path, so a rename is never committed. The name + persona themselves live
in `.env`, which is already gitignored.

> **specs/0037** promotes the print-only `$PROFILE` step to an automatic **registration** (`--set-name`
> appends the dot-source line; `--remove-name` removes it). Pass `--no-profile` to keep this print-only
> behavior.

`src/installshim.py` holds the PURE logic (no filesystem / PATH / clock / `sys.executable` — every input is
an argument), so it is unit-testable and importing it never pulls `litellm`/runtime/model:

- `validate_name(name)` — allowlist `^[A-Za-z][A-Za-z0-9_-]{0,31}$`, reject Windows reserved device names
  (`CON`/`PRN`/`AUX`/`NUL`/`COM1-9`/`LPT1-9`, case-insensitive, incl. trailing-dot/space variants), and reject
  `openagent-code`/`oac`/`OAC` (they collide with the existing command + are the default). Raises `ValueError`.
- `compute_env_update(env_text, name, persona)` — pure string→string, in-place-or-append set of
  `CODE_AGENT_NAME` / `CODE_AGENT_PERSONA`, omitting the persona line when empty and removing BOTH when
  `name` is falsy (the `--remove-name` revert). Preserves every untouched line and is idempotent.
- `plan_launcher(...)` / `plan_remove(...)` — return the launcher path, its content (absolute exe/python
  embedded, never a bare `python`), the `$PROFILE` line, and (POSIX) the chmod bits.

The `--set-name` / `--remove-name` subcommands are **set-and-exit**: `cli.main` dispatches them from the
LEADING argv token BEFORE `_parse_flags` / `Permissions` / MCP `connect()` / `warm_up()`, so they do zero
network I/O and never need a configured endpoint. If `--set-name`/`--remove-name` appears anywhere but the
leading token, `main` returns a usage error rather than letting it become a task prompt (**trap D**).

## Acceptance (each is a check in `scripts/check_naming.py`)

1. Default (`AGENT_NAME=OAC`, `AGENT_PERSONA=""`) → `build_system_prompt` renders `"You are OAC,"` and the
   banners read `OAC`.
2. Empty persona appends byte-nothing (no stray `\n\n`); a set persona appends its single line.
3. A blank/whitespace `CODE_AGENT_NAME` coalesces to `OAC` (never `"You are ,"`).
4. A custom name substitutes the ONE identity line; `config.agent_name()` is the shared value both banners read.
5. A persona containing `\n` is collapsed to one line and length-capped.
6. `plan_launcher` returns the right filename/body/mode per injected windows/exe/target (POSIX body has no
   `\r`, mode `0o755`, embeds an ABSOLUTE interpreter, never bare `python`; Windows body is a `function
   <name>` mirroring `oac.ps1`).
7. `compute_env_update` is idempotent (double-apply == single), preserves pre-existing lines, omits the
   persona line when empty, and removes BOTH vars on the revert.
8. `validate_name` rejects `../evil`, `a b`, `C:\x`, `rm -rf`, ``, `con`/`nul`/`com1`, and
   `openagent-code`/`oac`/`OAC`.

## Non-goals (follow-ups)

- Auto-appending the `$PROFILE` line (default is to PRINT it; an opt-in auto-append is a later addition).
- A cross-shell launcher beyond PowerShell + a POSIX shell script.
- Re-stamping the safety fingerprint with the agent name.

## Byte-identity

`CODE_AGENT_PERSONA` default empty → nothing appended (byte-identical to today's tail). The name substitution
is the one intended change: the default flips the identity line + banners from `openagent-code` to `OAC`
(the package/import name is untouched). Setting `CODE_AGENT_NAME=openagent-code` explicitly restores the old
literal exactly. `docker/code/Dockerfile` is a **deliberate non-edit** (same rationale as specs/0035): its
ENV pins only operational defaults and omits cosmetic flags, and the container is a headless `python -m src`
one-shot with no PATH-launcher use case.
