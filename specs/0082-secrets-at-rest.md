# 0082 — secrets at rest (ACL-lock + DPAPI vault)

Status: implemented
Flags: `CODE_LOCK_SECRETS` (default false), `CODE_LOCK_SECRETS_PATHS` (default `.env`),
`CODE_SECRETS_VAULT` (default false), `CODE_SECRETS_VAULT_PATH` (default `<install>/secrets.dat`)

## Goal

Phase 3 of the Codex-vs-OAC security adoption: protect the model credential AT REST on Windows. OAC keeps
`CODE_API_KEY` as plaintext in `.env`, created with default INHERITED ACLs — readable by any process running
as the user, and trivially exfiltrated by a prompt-injected `read_file .env` (that read is separately denied by
specs/0078, but the plaintext-on-disk exposure remains). Two controls, both stdlib + Windows built-ins, **no
pywin32 dependency**:

- **#9 ACL-lock** (`CODE_LOCK_SECRETS`): at startup, run `icacls` on each path in `CODE_LOCK_SECRETS_PATHS` to
  strip inheritance and grant Read to the current user + Full to SYSTEM — the Windows equivalent of `chmod 0600`.
- **#13 DPAPI vault** (`CODE_SECRETS_VAULT`): keep the key in a DPAPI-encrypted `secrets.dat` instead of
  plaintext `.env`. DPAPI (crypt32 `CryptProtectData`/`CryptUnprotectData`, via ctypes) ties the ciphertext to
  the user account with NO key material to store. At startup the vault is decrypted and injected into
  `os.environ` (setdefault), after which env-scrub (specs/0078) keeps it out of `run_command` children.

## Concepts

- **`src/secretsvault.py`** (new, dep-free):
  - `icacls_argv(path, user=None)` — pure: the owner-only `icacls` argv (`/inheritance:r` + `/grant:r <user>:R`
    + `/grant:r SYSTEM:F`). `lock_file_acl(path)` runs it (Windows only; no-op elsewhere; never raises).
  - `dpapi_encrypt`/`dpapi_decrypt` — ctypes/crypt32 DATA_BLOB round-trip. `_crypt()` raises `Unavailable` off
    Windows **or when ctypes can't load** (a sandboxed interpreter may ship a partial `_ctypes` with no
    `dlopen`); callers treat `Unavailable` as "feature off". `available()` = "DPAPI round-trips on this host".
  - `set_secret`/`get_secret`/`_read_vault` — a DPAPI-encrypted JSON blob. `load_into_env(path)` injects every
    vault secret into `os.environ` WITHOUT clobbering a value already present (setdefault); returns the count.
- **`cli._apply_secrets_startup()`** — called first thing in `main()`, gated on the flags, best-effort (never
  breaks launch). Loads the vault (re-reading `config.API_KEY` from the env afterward, since `config` was
  imported before the load) and ACL-locks the configured paths.

## Acceptance

`scripts/check_secretsvault_0082.py` (9/9, dep-free): `icacls_argv` construction; the non-Windows fallback
(ACL-lock no-op, `available()` False, `dpapi_encrypt` raises `Unavailable`); and — where DPAPI is actually
available — the live encrypt/decrypt round-trip, an on-disk vault that is genuinely encrypted (plaintext value
absent from the file bytes), and `load_into_env` setdefault semantics (injects a missing key, never clobbers an
existing env value). The live round-trip is skipped (with a printed note) where ctypes can't load, so the
harness passes both on the operator's real Windows host and in the sandbox.

## Non-goals

- Cross-platform DPAPI. On the Linux training substrate the vault is simply `Unavailable` and the feature is
  off — the model key there comes from the environment as before.
- Automatic migration of an existing plaintext `.env` into the vault. Populating `secrets.dat` is an explicit
  operator step (`set_secret`); this spec only *reads* a vault the operator has created and *locks* the files.

## Byte-identity

All four flags default OFF: `_apply_secrets_startup` is a no-op (no icacls, no vault load, env untouched), so a
default launch is byte-identical. Verified: full dep-free suite green with the flags off.
