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

### What "good" looks like in the digest
No `[REPETITION-LOOP]`, no `[REASONING-LEAK]`, no unexplained `[THRASH]`; gates fire when they should
(`[FALSE-DONE?]` = the completion gate *working*); honest outcomes.

### Known findings to watch (recurring, not yet fully fixed)
- **Rambling-CoT leak** — a long "However… but maybe… thus the final answer…" dump before the real
  answer. STILL fires despite the `ANSWER DIRECTLY` prompt rule (prompt lever isn't enough) — the next
  lever is a `looks_like_deliberation` detector + a rubric penalty so the flywheel selects against it.
- **Absence-claim grounding can still miss** — a run once answered "the auth service has no Go source"
  when `src/auth` holds 14 `.go` files. The completion-gate hijack (now fixed) was blocking the grounding
  gate from even running that turn; separately, searching a `--add-dir` reference dir by absolute path
  may not reach the files. Verify absence claims land, and build the grounding **read-ledger** so "X is
  empty" is contradicted by what the session actually listed/read.
- **Proportionality drift** in a long *resumed* session — a simple question can draw a review-sized answer.

## Environment prerequisites
- **Node.js/npm installed** — else centpilot's type-check/test `run_command` steps FAIL (not an OAC bug).
- centpilot has a stray **duplicate tree `src/homepage/src/homepage/`** — delete it; it lures the agent
  into editing the wrong copy.
