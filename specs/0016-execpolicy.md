# 0016 — execpolicy (parse run_command, gate on the parse)

## Goal
Gate `run_command` on what it actually DOES, not on its first token. The permission engine matches a
command against rules with a raw prefix matcher (`_match_command`), which sees only the leading token:
`cd src && rm -rf x` reads as `cd`, so a `deny run_command(rm:*)` rule never fires, and `git status`
prompts in default mode as if it were destructive. `execpolicy` parses a command line into SEGMENTS
(splitting on `&&`, `||`, `;`, `|`, newlines; unwrapping `$(...)` / backtick substitutions), classifies
each as **read_only / mutating / dangerous**, and lets the gate reason per segment. Additive precision
that also feeds the sandbox (0017) and guardian (0019).

Shell-aware: bash and Windows **PowerShell 5.1** (where `&&` / `||` are not valid operators — the model
emits them anyway; the live ride showed `cd src && npm test`). Pure, dependency-free, and it NEVER
raises — a line it can't parse degrades to one opaque `mutating` segment (the conservative default).

## Acceptance
- `src/execpolicy.py`: `split_segments(cmd, shell)` splits at top-level operators respecting quotes and
  pulls `$(...)`/backtick contents out as their own segments; `classify(segment, shell)` → one of
  `read_only` / `mutating` / `dangerous`; `assess(cmd, shell)` returns the worst class + per-segment
  classes + the dangerous segments + a `ps_invalid` flag (`&&`/`||` in a PowerShell command). Never raises.
- Classification: read-only (ls, cat, grep, find, git status/log/diff, Get-*/Test-Path, …), mutating
  (mv, cp, mkdir, sed -i, git add/commit, npm install, …), dangerous (`rm -r*`, `dd`, `mkfs`, `chmod -R`/
  777, `git push --force`, `git reset --hard`, a bare piped shell `| sh`, `iex`, `Remove-Item -Recurse`,
  a fork bomb, shutdown/reboot, …). An UNKNOWN command → `mutating` (conservative).
- Gate integration (`src/permissions.py`, behind `CODE_EXECPOLICY`): deny/ask/allow rules match **any
  segment**, so `run_command(rm:*)` blocks `cd x && rm y`; a wholly **read-only** command is allowed like
  a read tool (even in plan/default mode — `git status` no longer prompts). `dangerous` is not relaxed.
- **Flag OFF (default) is byte-identical to today** — decide() never consults execpolicy, so the prefix
  matcher path is unchanged.
- `scripts/check_execpolicy.py` proves splitting, subshell unwrap, per-class classification, the
  compound `cd x && rm -rf /` → dangerous, PS-5.1 `&&` flagged, per-segment deny firing, and flag-off
  parity. Dep-free, no model, no network.

## Non-goals
- Executing anything (this only CLASSIFIES; run_command still runs in tools.py).
- FS/network confinement — that is the sandbox (0017).
- Elevating a `dangerous` command to a fail-closed LLM review — that is the guardian (0019); here it is
  only classified and surfaced, not blocked beyond today's run_command gating.
- A complete shell grammar. Pragmatic coverage of common cases + a conservative default; extensible via
  `CODE_EXECPOLICY_DANGEROUS`.

## Notes
- Off by default (`CODE_EXECPOLICY=false`), like every adoption-track phase.
- The classifier is data (sets + patterns), so new commands are a one-line addition, not new logic.
