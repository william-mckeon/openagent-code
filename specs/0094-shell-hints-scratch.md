# 0094 — extra shell hints + scratch-file discipline

Status: implemented
Flag: `CODE_SHELL_HINTS_EXTRA` (default OFF → byte-identical). Needs `CODE_SHELL_HINTS` + `CODE_SITUATIONAL_CONTEXT`.

## Problem

Two "smaller notes" from the full-Inkling log `98d6cbd9d8a2`, separate from the "doesn't respond" fix (0093):

1. **PowerShell footguns still land.** The model ran `ls -la`, `cat << 'EOF'` (a heredoc), `tail`, `which` — all
   of which fail on PowerShell 5.1. Two blind spots:
   - **Heredocs** (`cat << 'EOF'`, `<<`) aren't covered by *either* the full or the lean shell block.
   - The **lean** shell block (specs/0090, which is armed) deliberately dropped the Unix-alias catalog "a model
     already knows" — but this model doesn't, so `ls -la` / `head` / `tail` / `which` regressed out of the hints.
2. **Scratch files left in the project.** The agent extracts PDFs into the workspace (`pdf_extract.txt`,
   `pdf_extracted_full.txt`, `detailed_report.txt`) and doesn't clean up, leaving temp files in the user's repo.

## Fix

One additional env-block line, gated on `CODE_SHELL_HINTS_EXTRA` (and only on Windows with `shell_hints` on), added
in `envcontext.build_env_context(extra_hints=…)` and threaded from `agent.py`
(`extra_hints=config.SHELL_HINTS_EXTRA`). The line covers exactly the gaps:

- **No heredoc**: `cat << 'EOF'` / `<<` FAIL; write a file with `Set-Content` or a small Python script; a
  multi-line literal is a `@'...'@` here-string with the closing `'@` at column 0.
- **Unix commands/flags**: `ls -la` / `ls -l` → `Get-ChildItem`; `which x` → `Get-Command x`; `head` / `tail` →
  `Select-Object -First N` / `-Last N` (restored for the lean block, which dropped them).
- **Scratch-file discipline**: put a PDF extraction / temp report under `$env:TEMP`, not the workspace — or delete
  it when done; don't leave scratch files in the user's project.

It's a **new** flag (not folded into `CODE_SHELL_HINTS`) so an existing `CODE_SHELL_HINTS`-only setup is byte-identical.

## Acceptance

`scripts/check_shell_hints_extra_0094.py` (dep-free, Windows-gated for the block assertions): with the flag ON the
extra line is present and names the heredoc trap, the `ls -la` / `which` / `head`/`tail` mappings, and the
`$env:TEMP` scratch discipline; with the flag OFF the env block is byte-identical (no extra line), independent of
lean/full. No regression across the situational/env suite.

## Non-goals / caveats

- This is a **soft** nudge. Per the Inkling behavior notes, the model sometimes ignores shell guidance; a prompt
  hint can't guarantee compliance, but it's the only lever short of intercepting `run_command` writes. A harder
  scratch-file guarantee (e.g. redirecting workspace writes) is out of scope.
- Does not change `run_command` execution, the non-interactive shell guard (0055), or the lean/full block bodies;
  it only appends one gated line.
