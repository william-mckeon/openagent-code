# 0046 — shell-hints

Status: implemented
Flag: `CODE_SHELL_HINTS` (default off; needs `CODE_SITUATIONAL_CONTEXT` on to take effect)

## Goal

Stop the agent from wasting turns on POSIX commands that fail under Windows PowerShell 5.1. A model trained
mostly on bash reaches for Unix idioms the shell rejects; the first live Inkling run on Centpilot burned
three separate turns before recovering:

- `mkdir -p src public/assets deploy/k8s` → failed (no `-p`; `mkdir` is `New-Item`)
- `curl -s -o /dev/null -w "%{http_code}..."` → failed (`curl` is an `Invoke-WebRequest` alias; no `/dev/null`)
- `docker rm -f centpilot-test && docker run ...` → failed (`&&` is a parse error in PS 5.1)

The situational block already tells the model `shell: PowerShell` but gives it none of these rules. This
adds them, at the source, so the model conditions on the shell's real syntax instead of failing then
recovering.

## Concepts

- **One gated line in the existing env block.** `build_env_context()` gains a `shell_hints` parameter; when
  it is on AND `os.name == "nt"` (the shell is actually PowerShell), it appends a single dense
  `- shell rules (PowerShell 5.1): ...` line covering the exact failure modes seen: chain with `;` not
  `&&`/`||`; `New-Item -ItemType Directory -Force` not `mkdir -p`; `$null`/`Out-Null` not `/dev/null`;
  `curl`/`wget` are `Invoke-WebRequest` aliases (call `curl.exe` for real curl, drop `-o /dev/null`); prefer
  `Invoke-WebRequest ... | Select-Object`.
- **Kept out of config.** `envcontext.py` stays pure stdlib + injectable (its harness needs no config); the
  flag is read in `agent.py` and passed as `shell_hints=config.SHELL_HINTS`, mirroring `include_git`.
- **PowerShell-only.** On bash the POSIX commands are correct, so the rules are appended only on Windows —
  no noise for POSIX users even with the flag on.

## Acceptance

Each item is an assertion in `scripts/check_situational.py` (14/14, dep-free, no model).

- `shell_hints` off (default): the block carries no `shell rules` line (byte-identical to pre-0046).
- `shell_hints` on + Windows (`os.name == "nt"`): the `shell rules (PowerShell 5.1)` line is present and
  names `New-Item`, `&&`, and `/dev/null`.
- `shell_hints` on + non-Windows: no rules line (PowerShell-specific).
- `CODE_SHELL_HINTS` defaults False when unset.

## Non-goals

- No bash/zsh hint set — POSIX shells run the model's default commands correctly.
- Does not enforce anything — it is a prompt hint; a wrong command still fails and is caught by the existing
  retry, this just makes the wrong command rarer.
- Not a substitute for `CODE_SITUATIONAL_CONTEXT`; the rules live inside that block, so the block must be on.

## Byte-identity

`CODE_SHELL_HINTS` off (default) means `build_env_context` appends nothing, so the env block, its pin, and
its trajectory capture are byte-for-byte what they were before specs/0046 (verified: `check_situational`
byte-identity assertion; `SCHEMA_VERSION` unchanged; the flag is not in `safety_fingerprint`).
