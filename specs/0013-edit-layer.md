# 0013 — Edit-layer (safe fuzzy fallback + atomic apply_patch)

> A SAFE fuzzy fallback UNDER the exact-match `edit_file` (recover a whitespace/indentation-drift miss,
> but only at a UNIQUE, above-threshold location — any ambiguity refuses), then an atomic,
> grammar-validated `apply_patch` tool for multi-file Add/Update/Delete/Move. Phase 13 of the adoption
> track (ROADMAP). All patterns are our own Python; no external agent is referenced.

## Why

`edit_file` is exact-match-or-fail on purpose (`src/tools.py` docstring): it forces the model to ground
every edit in text it actually read, fails loudly instead of silently corrupting, and a failed edit is
the cheapest training signal. That is the right default — but the weak model wastes real trajectory on
edits that miss only by trivial whitespace/indentation drift, and it has no atomic way to make a
coordinated multi-file change (each `edit_file` is a separate, partially-failing turn).

This phase adds two things WITHOUT weakening the invariant:
1. a fuzzy **fallback** that fires only when the exact match found nothing, and applies only a unique,
   high-confidence location (refuse-on-ambiguity), so it never silently corrupts;
2. `apply_patch` — one grammar-validated, **atomic** (all-or-nothing) envelope for many files.

Both route every touched path through the existing mutation ledger, so the completion gate (specs/0007)
and the grounding gate (specs/0010) already cover them with no change.

## Sub-phases (each independently shippable, behind its own default-off flag)

- **A — fuzzy fallback** (`CODE_EDIT_FUZZY`): `src/editmatch.py` — a graded cascade
  (exact → whitespace/indentation-insensitive → most-similar contiguous chunk via `difflib`) that
  returns a match ONLY when the location is unique and above `CODE_EDIT_FUZZY_THRESHOLD`; a tie or a
  low score refuses. `edit_file`'s `count==0` branch delegates to it when the flag is on and otherwise
  returns today's teaching error verbatim. Acceptance: `scripts/check_edit_fuzzy.py`.
- **B — apply_patch** (`CODE_APPLY_PATCH`): `src/patch.py` — a parser + grammar-validator + atomic
  applier for an Add/Update/Delete/Move envelope. Validate + resolve every hunk in memory first
  (Update hunks resolve via the sub-phase-A cascade), then write all-or-nothing; on any parse/hunk
  error, touch zero files. Registered as a gated `PATCH_TOOLS` group. Acceptance:
  `scripts/check_apply_patch.py`.
- **C — ledger/grounding conformance + touched-path manifest**: `apply_patch` records every touched
  path via `_record_mutation` (Move = delete(old) + write(new)) and returns a "Touched paths:" manifest
  so the closing answer can cite each path without a phantom-citation false flag. Acceptance:
  `scripts/check_patch_grounding.py`. (Not a flag — a conformance guarantee riding A+B.)

## The invariant (why this is safe)

Exact-match stays the FIRST strategy. The fuzzy fallback and every `apply_patch` hunk return a location
ONLY when it is UNIQUE — two equally-good spots, or a best score below threshold, resolve to a refusal
that keeps `edit_file`'s existing teaching error. So a genuine miss still fails loudly and still teaches;
only the trivial whitespace-drift miss is recovered. `apply_patch` is all-or-nothing, so there is never
a partial multi-file write. `CODE_EDIT_FUZZY_THRESHOLD` is the knob to keep conservative — a too-loose
threshold is what would reintroduce the silent-corruption risk.

## Acceptance (checkable)

- [ ] `scripts/check_edit_fuzzy.py` (dep-free): exact wins first; a whitespace/indentation-only miss
      applies at the correct span; a most-similar miss applies; TWO equally-good spots REFUSE with no
      write; a below-threshold garbage `old_string` REFUSES; flag off → today's exact-or-fail verbatim.
- [ ] `scripts/check_apply_patch.py` (dep-free): grammar rejects a malformed envelope (no write);
      Add/Update/Delete/Move each land; ATOMICITY — a patch whose one hunk fails leaves every file
      byte-unchanged; the tool is absent from `active_tools()` when `CODE_APPLY_PATCH` is off, present when on.
- [ ] `scripts/check_patch_grounding.py` (dep-free): every touched path is in `ctx.mutations` with the
      right action (Move → delete + write), satisfies `agent._unverified_items`, and clears
      `grounding.problems()` for an answer citing those paths; the manifest lists every touched path.

## Non-goals

- **Reindenting the replacement** to match the located whitespace — v1 replaces the located span with
  `new_string` verbatim (the model supplied the intended content); reflowing is a later refinement.
- **Loosening the default** — both flags default OFF; exact-match-or-fail is unchanged until opted in.
- **A trajectory schema bump** — the new tools ride the existing mutation ledger + `tool_call` records.

## Notes

- **Offline-curator follow-up (deferred, not built here):** the RUNTIME grounding gate needs no change
  (it reads `ctx.mutations`). But the OFFLINE curator's oracle `grounding.touched_paths` / `_ENGAGED`
  (Phase 11, `train/curate.py`) only recognizes `{read_file, write_file, edit_file, delete_file}` with a
  single `path` arg. `apply_patch` is a multi-hunk envelope with no single `path`, so if an `apply_patch`
  trajectory were ever curated (`CODE_CURATE` is off by default), its touched files would read as
  never-engaged and a real citation could be flagged a phantom. When apply_patch trajectories are
  curated, `_ENGAGED`/`touched_paths` (or `curate.py`) must learn the manifest. Tracked, out of scope for 0013.
- The `src/tools.py` module docstring is amended (not removed): exact-match stays FIRST and
  refuse-on-ambiguity is called out as preserving "never silently corrupt."

## Files

- **ADD** `src/editmatch.py` (A), `src/patch.py` (B/C), `specs/0013-edit-layer.md`,
  `scripts/check_edit_fuzzy.py` (A), `scripts/check_apply_patch.py` (B), `scripts/check_patch_grounding.py` (C).
- **UPDATE** `src/tools.py` (docstring amend + `edit_file` fuzzy delegation + `PATCH_TOOLS` + lazy
  `apply_patch` import), `src/toolset.py` (gate `PATCH_TOOLS`), `src/config.py` (`CODE_EDIT_FUZZY` +
  `CODE_EDIT_FUZZY_THRESHOLD` + `CODE_APPLY_PATCH`), `src/prompts.py` (advertise `apply_patch`),
  `.env.example` (the new flags), `README.md` (repo layout).
- **NO CHANGE (confirmed):** `src/agent.py` (`_unverified_items` reads `ctx.mutations`),
  `src/grounding.py` runtime gate (`problems`/`_exists` read `ctx.mutations`).
- **DELETE** none.
