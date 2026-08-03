# 0058 — no absence-verifier spawn on an empty workspace

Status: implemented
Flag: none new (rides `CODE_GROUND_SKIP_GREENFIELD`, specs/0042/0047)

## Goal

Stop the grounding gate from re-challenging "the workspace is empty" turn after turn on an EMPTY workspace.
On a fresh-start Centpilot run (empty dir) the agent answered "no `everydollar` file exists here" / "the
workspace is empty," the Tier-2 semantic verifier spawned to check the absence claim and returned "1 claim not
backed," the agent re-ran `Get-ChildItem` / `Measure-Object` to back it, and the loop repeated — dozens of
directory listings across the early turns, burning steps and tokens. The greenfield guard (specs/0042/0047)
already zeroes the PATH checks on such a dir, but the verifier still spawned for the ABSENCE claim (the code
even noted it "still runs for an absence claim, which [is] true... on an empty dir").

## Concepts

- **An absence claim on an empty dir is trivially true.** When the workspace is STRICTLY empty there is
  nothing that could contradict "no X here," so spawning a model reviewer to verify it is pure waste.
  `problems()` computes `empty_ws = greenfield and is_greenfield(cwd, 0)` (strictly zero reviewable files) and
  drops the absence claim from the verifier's spawn condition: `paths or (absence_claim and not empty_ws) or
  web_srcs`. On an empty dir with only an absence claim, no verifier spawns and `problems()` returns the
  (empty) deterministic result — no challenge, no re-listing.
- **Only strictly-empty; a scaffold still verifies.** The skip is gated on `is_greenfield(cwd, 0)`, NOT the
  wider `CODE_GROUND_GREENFIELD_MAX` range. A 1–15-file early scaffold still spawns the verifier for an
  absence claim, because "no config here" could be FALSE when files exist — so the honest-but-wrong catch is
  preserved for populated/scaffold dirs; only the trivially-true empty case is skipped.
- **The deterministic net still guards.** `absence_contradictions` (model-free, os.path-authoritative) runs
  regardless, so a genuinely false absence about a path that exists is still caught on any populated dir.
- **Cheap.** `empty_ws` is only evaluated when already greenfield, and the strict walk bails on the first
  file, so a populated repo pays nothing extra.

## Acceptance

New assertions in `scripts/check_grounding_paths.py` (17/17):

- Empty workspace + absence claim (semantic on, `CODE_GROUND_SKIP_GREENFIELD` on): NO verifier spawn (a
  recording stub is never called) and `problems()` returns `[]` — no challenge.
- A non-empty scaffold (`sub/real.py` present) STILL spawns the verifier for the same absence claim.
- `CODE_GROUND_SKIP_GREENFIELD` off: the empty-workspace absence claim spawns as before (byte-identical).

## Non-goals

- Not a change to the greenfield PATH behavior (specs/0042/0047) or the deterministic absence check.
- Not a widening to the whole `GREENFIELD_MAX` scaffold range — a scaffold's absence claims can be false, so
  they still verify.
- No new flag, no `SCHEMA_VERSION` bump, nothing added to `safety_fingerprint`.

## Byte-identity

`empty_ws` is `greenfield and …`, and `greenfield` is False whenever `CODE_GROUND_SKIP_GREENFIELD` is off — so
with the flag off the spawn condition is byte-for-byte the pre-0058 `paths or absence_claim or web_srcs`.
Verified by the flag-off assertion and the rest of `check_grounding_paths` / `check_grounding` unchanged.
