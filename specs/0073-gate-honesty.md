# 0073 — gate-honesty (six corpus-poison / gate-bypass fixes)

Status: implemented
Flag: none new (correctness fixes to default-on honesty gates + the trajectory scrubber)

## Goal

Close six verified bug-hunt findings where an honesty gate silently disabled itself or the scrubber leaked/
corrupted the corpus — the remaining corpus-poison cluster. Each lets a dishonest or wrong turn survive into
training data, the exact thing the flywheel must not ingest.

## The six fixes (all in the gate/scrub layer)

- **`ran_check` false positives** (`grounding.py`). The bare `build|compile|lint|type-?check` alternatives (and
  `npm\s+run`) matched `mkdir build`, `git checkout build`, `cat lint.log`, `Remove-Item build`, `npm run dev`
  — any exit-0 such command flipped `ctx._verified_ok` and silenced the unverified-success net for the whole
  turn, so a false "all tests pass" closed as `final`. Now `_CHECK_CMD` matches only distinctive test/lint/type
  tools, a build tool invoked as the COMMAND, or an explicit `<tool> <verb>` (`npm run build`, `go build`).
- **`ran_healthcheck` false positives** (`grounding.py`). The bare `http[s]?://` / `localhost:\d` alternatives
  matched any command containing a URL, so `git clone https://…` / `pip install -i https://…` flipped
  `ctx._runtime_ok` and disabled the specs/0053 runtime-done net. Now `_HEALTHCHECK_CMD` matches only an actual
  probe tool (curl / iwr / Test-NetConnection / nc / …).
- **A timed-out verifier logged as a PASS** (`verify_edits.py`). `_default_run_fn` caught
  `subprocess.SubprocessError` (which includes `TimeoutExpired`) and returned `(True, "")`, so a verifier that
  hung past `CODE_VERIFY_TIMEOUT` was recorded as a passing verification reward and flipped `_verified_ok` —
  worst exactly when the code was likely broken. Now a `TimeoutExpired` returns `(False, "timed out …")` (a
  failed check); a genuine `OSError` (missing binary) still fails OPEN.
- **Windows backslash citations invisible to grounding** (`grounding.py`). `_QUOTED`'s char class lacked `\`,
  so a backtick citation `src\main.py` produced no cited path — silently skipping the phantom-path check,
  `absence_contradictions`, and the Tier-2 verifier list on the Windows-primary project. Added `\` to the
  class (`_norm` already maps `\` → `/`).
- **Unquoted secrets leaked into the corpus** (`scrub.py`). The token-assignment pattern required a QUOTED
  value, so `.env` / YAML / shell-export secrets (`API_KEY=…`, `password: …`, `export API_KEY=…`) passed
  through verbatim with `CODE_SCRUB_TRAJECTORY` on. Added an unquoted-value branch (≥6 non-space chars, a
  quote-lookahead so the quoted branch still owns that case) — prose like `password: use a strong one` is left
  alone.
- **Card regex false positives** (`scrub.py`). `(?:\d[ -]?){15}\d` matched a solid 16-digit epoch timestamp and
  arbitrary space-separated digit runs, replacing real tool output with `[redacted:card]`. Now it requires the
  grouped 4-4-4-4 form (a separator between groups), so an epoch / id no longer false-matches.

## Acceptance

`scripts/check_gatehonesty_0073.py` (10/10, dep-free), no regression in `check_grounding` (48/48),
`check_runtime_done` (31/31), `check_scrub` (23/23), `check_verify_edits` (16/16), `check_verify_gate` (23/23):

- `ran_check`: the non-check commands are false; real checks (pytest / npm run build / go build / tsc / make
  test / eslint / …) stay true.
- `ran_healthcheck`: a bare URL command is false; a real probe tool is true.
- backslash and forward-slash citations are both seen.
- unquoted + quoted secrets are redacted, prose is left alone; a grouped card is redacted, an epoch is not.
- a verifier timeout returns `(False, …)`.

## Non-goals

- Not a rewrite of the grounding nets or the scrubber — targeted regex/handler corrections. `make` (bare) is
  still treated as a build (it almost always is). A missing-verifier `OSError` still fails open by design.
- `ran_check`/`ran_healthcheck` narrowing errs toward NOT flipping the verified/runtime flags — a missed real
  check just means the unverified-success net runs (correct default), never a false "verified".

## Byte-identity

Each fix only changes classification for the inputs that were previously mis-handled; legitimate checks,
probes, citations, and secrets are unchanged. The scrubber changes ride `CODE_SCRUB_TRAJECTORY` (off →
byte-identical). Verified: full dep-free suite 56/56.
