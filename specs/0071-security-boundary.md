# 0071 — security-boundary hardening (four bug-hunt findings)

Status: implemented
Flag: none new (correctness/security fixes to existing default-on boundaries)

## Goal

Close four verified security-boundary holes from the full bug hunt. All are default-on paths (no flag needed to
reproduce) — a porous fence, a bypassable self-kill guard, a grant whose enforcement is wider than its label,
and a defense layer with a name-matching gap.

## The four fixes

- **`print_tree` escaped the workspace fence** (`tools.py` alias resolved AFTER the gate). The permission
  engine keyed the fence on the raw tool name; `print_tree`→`tree` was resolved later, inside `Registry.run`,
  so `print_tree` was classified as an unknown tool, got a fence-free read-only ALLOW, and could enumerate any
  directory on the host. Fix: `permissions.decide` canonicalizes the tool name through `_TOOL_ALIASES`
  (`permissions._canonical_tool`, lazy import to avoid the tools↔permissions cycle) BEFORE `_target`, so the
  alias is classified and fenced exactly like `tree`.
- **The self-kill guard was bypassed by the idiomatic PowerShell pipe** (`permissions.py`). The old regex
  required the kill verb BEFORE `python` in one segment and stopped at `|`, so `Get-Process python |
  Stop-Process` slipped through and killed the agent's own `python -m src`. Fix: split the command into
  STATEMENTS (`; & newline && ||` — a pipe is NOT a separator) and flag a statement that contains BOTH a kill
  verb AND a `python[3w]?` token in EITHER order. Now the pipe form and `taskkill /IM python3.exe` are caught,
  while `Stop-Process -Name foo; python x` (two statements) and kill-by-PID stay safe.
- **`/add-dir` said "granted (read)" but granted WRITE** (`cli.py`). It appended to write-capable `extra_roots`,
  and the acceptEdits/bypass baseline auto-allows `write_file` there — so a "read-only" reference repo could be
  silently edited. Fix: route the grant to `read_only_roots` (mirroring `_repl_grant_readonly`), so enforcement
  matches the message and the model's belief: reads widen, writes stay denied.
- **The goal entry filter missed versioned interpreters** (`goal.py`). `entry_ok` refused `python -c <code>`
  but an exact-set test let `python3.12 -c`, `pythonw -c`, `nodejs -e`, `python.exe -c` through — a bypass of
  one of the four defense layers on a model-proposed, unattended, re-run-every-iteration bar. Fix: match
  versioned/alternate names with `_INTERPRETER_RE` (`python[0-9.]*w?`, `node(js)?`, …, optional `.exe`).

## Acceptance

`scripts/check_security_0071.py` (10/10, dep-free), plus no regression in `check_self_preservation` (6/6),
`check_permissions` (36/36), `check_goal` (29/29):

- self-kill: the pipe form and `taskkill /IM python3.exe` flag; direct forms still flag; a kill of another
  process beside an unrelated `python` call, and kill-by-PID, do NOT flag.
- fence: `tree` AND `print_tree` are both denied outside the workspace; `print_tree` inside is still allowed.
- `/add-dir`: a read-granted dir denies writes in acceptEdits and allows reads.
- entry_ok: versioned/alt interpreters with an inline-code flag are refused; a legit non-inline bar passes.

## Non-goals

- Not a redesign of the fence or the guardian ladder — targeted classification/matching fixes.
- The self-kill guard remains name-based (kill-by-PID is intentionally allowed); it is defense-in-depth under
  deny-rules + the fence, now without the pipe hole.

## Byte-identity

Each fix only changes the decision for inputs that were previously mis-handled (an aliased out-of-fence
listing, a piped self-kill, a mislabeled write grant, a versioned-interpreter inline bar). Legitimate calls are
unchanged. Verified: full dep-free suite green.
