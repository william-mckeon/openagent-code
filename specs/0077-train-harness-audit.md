# 0077 — train / harness-audit (four bug-hunt findings)

Status: implemented
Flag: none new (a `train/curate.py` correctness fix + three harness-quality fixes)

## Goal

Close the last bug-hunt batch: a corpus-curation false positive on Windows paths, plus three harness weaknesses
where the CHECKER itself was wrong — a tautology and two vacuous assertions that would pass even if the code
regressed. A test that can't fail is worse than no test; it advertises coverage it doesn't have.

## The four fixes

- **Curate flagged a Windows-discovered file as a phantom** (`train/curate.py`). `_seen_blob` (the conservative
  "was this path seen in any tool listing" net) lowercased the tool results but did NOT normalize backslashes,
  while cited paths ARE normalized to `/`. So a file discovered only via a PowerShell listing (`src\main.py`)
  didn't match its own `/`-normalized citation and the whole session was dropped from the corpus. Fixed with a
  `\` → `/` normalization, mirroring the citation side.
- **`check_scrub` opt-in check was a tautology** (`scripts/check_scrub.py`). It asserted
  `_as_bool(os.environ.get("CODE_SCRUB_TRAJECTORY", "false")) is False` — with `"false"` hardcoded as the
  default, that is ALWAYS true and never touches config; it would still pass if config flipped the default to
  `"true"`. Now it asserts the config SOURCE default literal is `"false"`.
- **`check_verify_gate` check 2 didn't test the corpus-critical property** (`scripts/check_verify_gate.py`). The
  fail-once-then-pass case asserted a passing reward was logged but never that the intermediate FAILURE was
  NOT — the specs/0014 "log only the final result" rule that keeps a failed-then-fixed run trainable. Now it
  asserts ALL logged records passed (no failing one survived).
- **`check_verify_gate` check 4 was vacuous** (`scripts/check_verify_gate.py`). "label off → no reward" was
  observationally identical to a SKIPPED gate (canned `_PASS`, empty `verifs` either way). It now also asserts
  `ctx._verified_ok` is set, proving the gate actually RAN (consumed the result) while logging no reward.

## Acceptance

The strengthened harnesses still pass — and now non-vacuously: `check_verify_gate` 23/23, `check_scrub` 23/23.
A backslash-listing regression added to `check_curate` (16/16): a `src\main.py` listing grounds a `src/main.py`
citation. Full dep-free suite 59/59.

## Non-goals

- The `check_scrub` opt-in check is now source-coupled (it matches the exact config.py line) — a deliberate
  trade for a MEANINGFUL assertion over a tautological one.
- Not a broader audit of every "defaults False when unset" check in the suite; only the one the hunt flagged.

## Byte-identity

`_seen_blob` only changes for a result containing a backslash (a Windows listing) — it now grounds a citation
that was previously a false phantom; no real phantom is newly grounded (the path must still appear in a
listing). The harness fixes are test-only. Verified: full dep-free suite 59/59.
