# 0027 - grounding accuracy (catch the phantom PRESENT-path the semantic verifier waves through)

## Goal
The log review found the grounding gate firing wrong in a way that *under*-catches: the agent described a
`careeragent-frontend/docker/frontend/Dockerfile` in detail while every read of it failed and the file did
not exist, and grounding stayed silent. Two mechanisms let a phantom PRESENT-path citation through, and a
third made the Tier-2 verifier unable to read a legitimately-cited granted-dir file. All three are closed
behind ONE default-off flag so a flag-off run is byte-identical.

## Concepts
- **The present-path check is skipped in the default (semantic) mode.** `grounding.problems()` runs the hard
  cited-path existence check (`deterministic_problems`) ONLY in the semantic-OFF branch. With semantic on
  (the default), the only path check is the Tier-2 verifier, which is FAIL-OPEN and instructed to flag only a
  claim a file *contradicts* - a claim describing a file that doesn't exist has nothing to read, so it slips.
  Fix: also run the deterministic os.path existence check IN the semantic branch (`_present_path_problems`).
- **The strict extractor drops extension-less filenames.** `cited_paths(strict=True)` requires a known dotted
  extension (`_EXT`), so `.../Dockerfile` is never extracted or existence-checked. Fix: `_NOEXT_FILES`
  recognizes well-known extension-less files (Dockerfile / Makefile / Rakefile / Gemfile / ...), added to the
  strict set via `_strict_paths(..., noext=True)`.
- **The verifier subagent isn't told the granted dirs.** `run_subagent` is the sole `build_agent` caller that
  omits `granted_dirs`, so the grounding verifier's prompt never lists a `--add-dir` / `request_dir` root -
  it can't read a cited granted-dir file by absolute path (its inherited fence would allow it). Fix: thread
  `granted_dirs=parent_ctx.permissions.extra_roots` into the child's `build_agent`.

## Acceptance
- `src/grounding.py`: `_NOEXT_FILES`; `_strict_paths(final_text, noext=False)` (strict set, +extension-less
  when `noext`); `_present_path_problems(final_text, ctx, noext=False)` (the existence check, factored out of
  the old inline semantic-off tail so `noext=False` reproduces it EXACTLY); `problems()` runs it in the
  semantic branch when `CODE_VERIFY_GROUNDING_PATHS`, and the semantic-off tail passes `noext=` the flag.
- `src/subagent.py`: `run_subagent` threads `granted_dirs=extra_roots` into `build_agent`, gated on the flag.
- `src/config.py` + `.env.example`: `CODE_VERIFY_GROUNDING_PATHS`, default false.
- `scripts/check_grounding_paths.py` - dep-free, no model / no network.
- **Flag OFF is byte-identical**: the existence check stays semantic-off-only, no extension-less names are
  recognized, and the verifier subagent's prompt is unchanged (`granted_dirs=None`).

## Traps (each is a test)
- **Byte-identical semantic-off.** `_present_path_problems(..., noext=False)` MUST equal the old inline
  strict-only existence check (same `_exists`: a bare basename is never hard-flagged; only a slash-path
  missing from disk AND the mutation ledger).
- **Extension-less only with the flag.** `_strict_paths(noext=False)` returns the plain strict set;
  `_NOEXT_FILES` matches only by basename (`(?:^|/)Dockerfile$`), so `Dockerfile.md` (has an ext) goes the
  normal route and `my_dockerfile_notes` doesn't match.
- **A bare `Dockerfile` (no slash) is never hard-flagged** - `_exists` returns True for a slash-less token
  (it could be a subdir file), exactly like every other basename.
- **The verifier fix is a lone omitter.** Every other `build_agent` call already passes `granted_dirs`; only
  `run_subagent` didn't. Gated so flag-off leaves the child prompt untouched.

## Non-goals (v1)
- Threading cwd/roots INTO `_verifier_task` to re-resolve a relative citation (two adversarial reviewers
  showed the verifier already inherits the parent cwd + `extra_roots`, so a workspace-relative citation
  already resolves against the right root; the real gap is the fail-open PRESENT-path check, fixed here).
- Teaching the semantic verifier to treat "I couldn't find the file" as ungrounded (fail-open is deliberate;
  the deterministic os.path check is the authoritative backstop).
