# 0042 — oac-run-fixes

Status: implemented
Flag: `CODE_GROUND_SKIP_GREENFIELD` (Fix B only; default off). Fix A is unconditional input validation.

## Goal

Two defects the live Centpilot build run exposed, both in OAC itself (not the model):

- **Fix A — an unvalidated `--mode` launch flag.** The operator launched with `--mode perpose` (a typo for
  `propose`). The flag parser stored the bogus string verbatim; `Permissions` never matched it to a real
  mode, so `config.PROPOSE` was never turned on and propose-mode auto-allow silently degraded into a
  per-write approval prompt — **27 individual prompts after a single approved manifest.** A mistyped
  permission mode must fail loud, not silently pick a weaker posture than the operator asked for.

- **Fix B — grounding mis-fires on a greenfield workspace.** On an empty project directory (no source
  files yet), the closing answer's cited paths are files the agent PROPOSES to create. The path-existence
  grounding — the deterministic present-path check and the Tier-2 path verifier — has nothing to ground
  against, so it flagged **42 then 15** proposed paths as phantom "unbacked" citations. The false flags are
  noise (they ship labeled, they do not block), but they bury real signal and burn verifier spawns.

## Concepts

- **Fix A lives at the single launch chokepoint** (`cli.main`, right after `_parse_flags`, before
  `Permissions.from_config`). An explicit `--mode` whose value is not in `_MODES` is rejected with exit 2, a
  hint at the nearest valid mode (`difflib.get_close_matches`, so `perpose -> propose`), and the full mode
  list. It is only ever a HINT — an unknown mode is never auto-corrected, because silently running a
  different permission mode than the one typed is the exact failure this guards against. An invalid
  `CODE_PERMISSION_MODE` **env** value keeps its existing back-compat fallback in
  `resolved_permission_mode()` — that is a config default, not this-run intent; only the explicit flag is
  hard-rejected.

- **Fix B is a scoped skip, not a weakening of the gate.** `grounding.is_greenfield(cwd)` is a bounded walk
  that returns True only when the workspace has no reviewable source file (skipping `.git`/venv/build/cache
  dirs and dotfiles), and returns the instant it finds one real file — so a populated repo pays a single
  readdir. When the flag is on and the workspace is greenfield, `problems()` skips ONLY the path checks: the
  deterministic present-path check is bypassed, and the Tier-2 verifier is handed no paths (so it does not
  spawn for a pure path-proposing answer). The success-claim, absence-contradiction, and web-citation nets
  still run. The guard is greenfield-only: the moment the repo contains any real file, a cited-but-missing
  path is a phantom again.

- **Invariant:** the guard changes behavior for exactly one case — a cited, not-yet-existing path on an
  empty workspace — and nothing else.

## Acceptance

Each item is a concrete assertion in `scripts/check_oac_fixes.py` (14/14; the `--mode` integration item runs
as a subprocess and needs the venv/litellm, skipped gracefully under system python).

- `is_greenfield` returns True for a dir holding only `.git` + `.gitignore`, False once an `app.py` exists,
  and False for a nonexistent path.
- **Flag OFF + greenfield**: a `I will create src/budget/allocations.py ...` answer still flags both
  proposed files (proves byte-identity to today).
- **Flag ON + greenfield**: the same answer produces no problems.
- **Flag ON + populated repo**: a cited-but-missing path is STILL flagged (guard is greenfield-only).
- **Semantic ON + greenfield**: the Tier-2 verifier is NOT spawned for a pure path-proposing answer; with
  the flag off on a populated repo it IS spawned (unchanged).
- `CODE_GROUND_SKIP_GREENFIELD` defaults False when unset (opt-in).
- `perpose` is not in `_MODES`; `python -m src --mode perpose <task>` exits 2, suggests `propose`, and lists
  the valid modes.

## Non-goals

- No new heuristic for "this answer is a plan vs a claim" beyond the workspace-empty signal — a partially
  populated repo whose answer proposes new files is deliberately left to the normal (flag-independent) path
  checks; over-reaching there risks suppressing real phantom catches.
- The model-quality issues from the same run (blind `docker compose` retry, malformed `pyproject.toml`) are
  flywheel corpus, not harness patches — out of scope here.

## Byte-identity

Fix B: `CODE_GROUND_SKIP_GREENFIELD` off (default) short-circuits before `is_greenfield` is ever called, so
grounding does zero extra I/O and every existing trajectory reproduces exactly (verified: `check_grounding`
48/48, `check_grounding_paths` 14/14, `check_patch_grounding` 5/5 unchanged). Fix A changes behavior only for
an invalid explicit `--mode`, a pre-existing bug path — every valid mode and every no-flag run is unchanged.
