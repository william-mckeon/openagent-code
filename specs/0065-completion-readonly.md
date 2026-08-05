# 0065 — the completion gate must not trap a read-only review

Status: implemented
Flag: none new (a correctness fix to the specs/0007 completion gate + a specs/0051 `CODE_PROMPT_HYGIENE` clause)

## Goal

Stop the completion gate from turning a READ-ONLY review into a workspace-vandalizing fabrication loop.

Seen live (Inkling-Small on Tinker, `CODE_SHOW_REASONING` made it visible): the user asked Arcus to *review*
a repo, *try to run it*, and *provide a list* of files to update/delete — a read-only task. Arcus reviewed
correctly and gave the list. But it had built an `update_plan` whose steps carried `file` values like `N/A`
and `Centpilot` (the workspace folder), marked `completed`. Read-only ⇒ nothing changed ⇒ the completion gate
fired: `completion challenged — 5 item(s) not backed by a real change`. The reasoning stream then showed the
misread verbatim — *"they want actual edits… to back up the list"* — and the agent:

1. fabricated edits to three `.cs` files it was never asked to touch,
2. wrote `Centpilot.csproj`, `Centpilot.sln`, `MISSING_BUILD_ASSETS.txt` from nothing,
3. when the gate fired AGAIN, wrote one junk file per plan step (`CONVERSATION_REVIEW.txt`, `FOLDER_REVIEW.txt`,
   `EXECUTION_ATTEMPT.txt`, `UPDATE_DELETE_LIST.txt`) purely to feed the gate.

The gate meant to stop a dishonest "done" *forced dishonest work*, and steamrolled the base-prompt rule that a
review is read-only. Two compounding causes:

- **The junk-`file` trap.** `_unverified_items` challenges any completed step with a truthy `file`. `"N/A"` and
  `"Centpilot"` are truthy but can NEVER appear in the mutation ledger, so the gate can never be satisfied — it
  just escalates. The more the agent fabricates, the more it complains.
- **No honest exit.** `_completion_challenge` only offered one resolution: *"make each change with edit_file /
  write_file / delete_file."* It never told the agent that a review step should simply drop its file.

This is the inverse of the usual small-model lesson: a deterministic gate injects a FACT the model obeys, but
here the fact pointed the wrong way. The fix is to make the fact CORRECT, not to weaken the gate.

## Concepts

- **A step is checkable only if it names a real file target.** New `agent._is_checkable_target(ctx, f)`: True
  iff `f` resolves to an actual file on disk (an edit that should have landed), is a path with a file
  extension, OR is a well-known extensionless file — `Dockerfile`, `Makefile`, `Gemfile`, … via grounding's
  `_NOEXT_FILES` allowlist (all create targets that should exist). A directory (`Centpilot`, `src/`) or a bare
  placeholder (`N/A`, `TBD`, a free-text review label — no extension, not a file, not in the allowlist) returns
  False. `_unverified_items` only appends the "nothing changed it" problem when the target is checkable;
  otherwise it skips silently. This kills the unsatisfiable `N/A`/`Centpilot` trap while PRESERVING the real
  cases — an edit to an existing file that didn't land (isfile → True), a create with a file extension, and a
  create of a well-known extensionless config file all still flag. The reused allowlist is the safe way to
  recover extensionless creates: an invented label can never match a fixed set of real filenames, so it cannot
  re-trap the gate. A residual, deliberate blind spot remains — a never-written extensionless create NOT in the
  allowlist (a bare `LICENSE`, a dotfile like `.gitignore`, an extensionless script) goes unchallenged; that is
  the correct direction to err (a missed over-claim, still often caught by the grounding net or a human reading
  the plan, is far less harmful than the fabrication loop this spec exists to kill), and the read-only-vs-create
  discrimination is undecidable from the label alone.
- **The challenge offers the read-only exit.** `_completion_challenge` now says: make the change *if the step
  genuinely changes a file*; but if it was a review / analysis / read-only step, DROP its file with
  `update_plan` — and NEVER create, edit, or delete a file just to satisfy the check.
- **The prompt forbids fabrication-to-satisfy.** The `CODE_PROMPT_HYGIENE` note gains a `(read-only integrity)`
  clause: a review/analysis changes nothing, its plan steps carry no file, and the agent must never write a
  file (including a scratch/notes file) to back up a step or satisfy an internal check — correct the plan
  instead.
- **The sibling manifest gate gets the same directory guard.** `_unapplied_manifest` skips an approved item
  whose `path` resolves to a directory (never a file mutation), so it can't spuriously report a folder target
  unapplied.

## Acceptance

New assertions in `scripts/check_verify_gate.py` (dep-free, extends the existing completion-gate harness):

- `_is_checkable_target`: a directory target and a bare `N/A`/`TBD` placeholder → False; an existing file and a
  `foo.py`-style create target → True.
- `_unverified_items`: a completed review step with `file="N/A"` (or `"Centpilot"`, or a real subdirectory) on
  an EMPTY ledger is NOT challenged (returns `[]`).
- `_unverified_items`: an existing-but-unmutated file step STILL flags (an edit that didn't land), a `foo.py`
  create target that never appeared STILL flags (a create that didn't happen), and a never-written `Dockerfile`
  / `docker/Makefile` create STILL flags (extensionless allowlist) — the gate keeps its honest-completion value.
- `_is_checkable_target`: a well-known extensionless file (`Dockerfile`, a slash-pathed `docker/Makefile`) is
  checkable; a `N/A` / free-text label / directory is not.
- `_completion_challenge(...)` text contains the read-only exit (drop the file with update_plan; never
  create/edit/delete a file to satisfy the check).
- `_unapplied_manifest`: an approved item whose `path` is a directory is not reported unapplied.
- All prior specs/0007 + specs/0026 assertions still hold (absolute-path backing, case-fold, per-task reset,
  the manifest reconciliation, the flag-off byte-identity).

## Non-goals

- Not a change to WHEN the gate runs (still behind `CODE_VERIFY_COMPLETION`, default on) or to the grounding /
  acceptance gates. Only what counts as a challengeable target and how the challenge reads.
- Not an attempt to auto-detect "this whole task is read-only" — undecidable from the task text. The fix is
  deterministic (real-target guard) plus an honest exit the model can take.
- No new flag, no `SCHEMA_VERSION` bump, nothing added to `safety_fingerprint`.

## Byte-identity

With `CODE_VERIFY_COMPLETION` off, the completion gate never runs — byte-for-byte unchanged. With it on, the
only behavior change is that placeholder/directory steps no longer trap the gate and the challenge text carries
the read-only exit; every real create/edit/delete verification is preserved. The `CODE_PROMPT_HYGIENE` clause
rides that existing flag; a flag-off prompt is byte-identical. Verified: `check_verify_gate` green, full suite
green.
