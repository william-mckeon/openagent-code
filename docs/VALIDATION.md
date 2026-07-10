# Validation — run after every change, and the FULL ride after every phase

Two layers. The **automated harnesses** are the fast, deterministic gate — run them on every change.
The **manual ride** is the live-model regression check — run the *whole* menu after each phase so an
older phase can't silently break, to see fixes fire repeatedly, and to find things to harden.

---

## 1 · Automated acceptance harnesses (no model, no network)

Every phase ships a dependency-free `scripts/check_*.py`. Run them all — each must end in `[OK]`:

```powershell
cd C:\Users\willi\OneDrive\Desktop\OpenCode
.\.venv\Scripts\Activate.ps1
Get-ChildItem scripts\check_*.py -Exclude check_native_toolcalls.py | ForEach-Object {
  Write-Host -NoNewline ("{0,-28}" -f $_.Name)
  python $_.FullName 2>&1 | Select-String 'VERDICT:'
}
```

If one `[FAIL]`s, re-run just that file for the per-check detail (e.g. `python scripts\check_grounding.py`).

The one **live** harness (needs the model endpoint up) is separate:
`python scripts\check_native_toolcalls.py`.

---

## 2 · Manual live ride (centpilot) — run the whole menu after each phase

Launch: `cd C:\Users\willi\OneDrive\Desktop\centpilot ; oac --mode acceptEdits`.
Reset between runs with `git checkout .` inside centpilot. Then digest the log:
`python skills\review-log\scripts\summarize_log.py logs\<session>.log` (or hand `logs\<session>.log` to a reviewer).

### Query menu (grouped by what each exercises)

**Situational-context (Phase 12)**
- `without using any tools, what is today's date, what directory are you in, and what OS/shell?`
  → instant, **zero tools**, correct **local** date.

**Fuzzy edit + repetition guard (Phase 13-A / degeneracy guard)**
- `fix the clsx import in Button.tsx (a default import, not a named one)`
- `rename the NavLink piece in Navigation.tsx to NavItem everywhere it's used`
  → the rename is the **repetition-loop trigger**: if it loops, expect a single
  `[degenerate repetition output detected and suppressed]` and the turn ends — **not** a wall of
  repeated lines, and **no** forced compaction on the next turn. Watch the indented edits for
  `Edited … (fuzzy match: whitespace)`.

**apply_patch (Phase 13-B)**
- `add a CONTRIBUTING.md at the repo root and add a "## Contributing" section to the README that links to it`
- `the favicon README lists icons the pages reference but are missing — wire the references into _document.tsx and align the names in site.webmanifest`
  → watch for `*** Begin Patch …` → `applied N operation(s) atomically`, or a clean `could not parse …`
  teaching error. A **binary Move/rename must NOT crash** (fixed).
  → **Fence (fixed):** every op is now re-gated through the permission engine, so a patch can NOT write
  outside the workspace or touch `.env`/`.git/**` (it was bypassing all of that). A **Delete/Move** op
  gates like `delete_file`, so in `acceptEdits` it PROMPTS (interactive) or blocks (headless) — use
  `--mode bypass` for a fully-unattended favicon-Move ride, or approve the delete when asked.

**auto-verify (Phase 14)** — needs a **Python target**: the default check is `python -m py_compile`, and
centpilot has no `.py`, so on centpilot auto-verify is a no-op unless `CODE_VERIFY_CMDS_CONFIG` maps its
languages to an *installed* checker. Cleanest test — a scratch python dir with a seeded syntax error:
```powershell
mkdir C:\Users\willi\OneDrive\Desktop\pyscratch -Force; cd C:\Users\willi\OneDrive\Desktop\pyscratch; git init | Out-Null
"def add(a, b)`n    return a + b" | Set-Content calc.py     # note: missing ':' -> a syntax error
oac --mode acceptEdits
```
- `add a subtract(a, b) function to calc.py`
  → the model edits `calc.py`, `py_compile` FAILS on the `def add(a, b)` error, you see
  `[verify] N touched file(s) failed the check`, and the reflection loop fixes the colon so it compiles.
  A run that never gets the file to compile ends `verify_failed_edits` (dropped from the corpus). A clean
  edit that compiles logs a silent pass-reward (visible in the trajectory, not the console).

**Regression — proportionality + review (session fixes)**
- `what project is this?` → ~2 tool calls + a **short** answer, not a full audit/essay.
- `review the whole project` → fans out via `review_repo`; no repeated `review_repo` runs; nothing
  called "empty" that it actually read.

**Cross-turn hijack (completion gate is now per-task)** — the sequence matters, run it in order:
1. `the favicon README lists icons the pages reference but are missing — wire the references into _document.tsx and align the names in site.webmanifest`  (leaves unbacked steps: the PNGs don't exist)
2. then `what project is this?`
  → turn 2 must answer **about the project** — NOT re-litigate the favicon plan, NOT a "changes I made"
  table, NOT "I exhausted my step budget". A prior task's completed-but-unbacked steps are reset each
  turn, so the completion gate can't carry into an unrelated question. Also confirm a real edit made via
  an **absolute path** (or a `--add-dir` reference dir) isn't falsely challenged as "not backed".

**Grounding honesty (absence-claim fix)**
- `does the auth service actually have Go source, or is it just docs and config?`
  → it must **look** (read the `.go` files) before answering, never confabulate "empty".

### Deviation matrix — ride OFF the happy path (where the seam bugs live)

The full-phase audit ([AUDIT-FINDINGS.md](AUDIT-FINDINGS.md)) confirmed 19 real bugs while every unit
harness was green — all in a **seam** the happy-path ride doesn't exercise. A seam bug is a **violated
assumption**; find them by deliberately breaking each assumption the code makes. Ride at least one query
through each row:

| Assumption the code makes | Deviation to ride | Watch for |
|---|---|---|
| the workspace **is** the edit target | launch in dir A, `--add-dir` B, edit B by absolute path | no false "not backed"; fence still blocks outside A+B |
| **one task** per session | favicon task (leaves unbacked steps) → then `what project is this?` | turn 2 answers the project, not a stale favicon status |
| a **fresh** session | `--resume <id>` an old session, then a new unrelated task | the prior plan doesn't hijack; no dangling-tool_call error |
| flags are **stable** per session | toggle `/mode` (or a `CODE_*` flag) mid-session | later turns honor the new mode; no stale pin |
| the repo is **small** | a big repo / low `CODE_COMPACT_AT_TOKENS` to force mid-turn compaction | a failed turn rolls back cleanly (no poisoned next turn) |
| files are **LF** | edit a CRLF file | the diff is one line, not a whole-file re-ending |
| `apply_patch` targets are **safe** | (CODE_APPLY_PATCH on) a patch that Deletes `.env` or writes `../out` | refused; a Delete/Move prompts in acceptEdits |

Every new deviation that surfaces a bug becomes a new `check_*.py` — the ratchet, not a one-off.

### What "good" looks like in the digest
No `[REPETITION-LOOP]`, no `[REASONING-LEAK]`, no unexplained `[THRASH]`; gates fire when they should
(`[FALSE-DONE?]` = the completion gate *working*); honest outcomes.

### Known findings to watch (recurring)
- **Rambling-CoT leak** — the "thus the final answer:" conclusion shape is now caught (`_CONCLUSION_META`):
  scored by `has_reasoning_leak` and stripped from SFT targets, so the flywheel selects against it. A
  free-form ramble *without* that conclusion marker ("However… but maybe…") is still not flagged by design
  (too false-positive-prone) — the flywheel + the `ANSWER DIRECTLY` prompt rule are the levers there.
- **Absence claims are now checked** — a prose-only "X has no source / is empty" spawns the Tier-2
  verifier even with no cited path (the "auth has no Go source" miss). Still confirm on a ride that the
  verifier actually *looks* and corrects a false absence; a deeper grounding **read-ledger** (contradict
  "empty" from what the session listed) remains a possible follow-up.
- **Proportionality drift** in a long *resumed* session — a simple question can draw a review-sized answer.

## Environment prerequisites
- **Node.js/npm installed** — else centpilot's type-check/test `run_command` steps FAIL (not an OAC bug).
- centpilot has a stray **duplicate tree `src/homepage/src/homepage/`** — delete it; it lures the agent
  into editing the wrong copy.
