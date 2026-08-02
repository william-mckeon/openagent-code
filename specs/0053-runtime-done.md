# 0053 — runtime-done honesty gate

Status: implemented
Flag: `CODE_VERIFY_RUNTIME_DONE` (default off)

## Goal

Stop the agent from declaring a running service healthy when it never reached it. On a live Inkling-Small
Centpilot run the model said "Done — plumbing fixed" while `curl localhost:8080` had just returned
connection-refused (the container it killed was never brought back up). Nothing caught it: stopping is
model-driven, the opt-in `pursue` bar was never declared, and the deterministic unverified-success net
(`grounding._SUCCESS`) only recognizes test / build / lint "pass / green / clean" language — it has no
vocabulary for "up / serving / plumbed", and it keys its "verified?" signal on CHECK commands
(pytest/tsc/…), not on a health probe. So a runtime success claim had no net at all.

## Concepts

- **A runtime twin of the unverified-success net.** `grounding.unverified_runtime_claim(final_text,
  runtime_verified)` flags an UNCONDITIONAL claim that a service / app / server / container / deployment is
  UP / running / serving / listening / reachable / healthy / "plumbed" / "wired up" when `runtime_verified`
  is False. It is deterministic and model-free, mirrors `unverified_success_claim` exactly (per-sentence,
  `_HEDGED`-guarded), and adds the general `_MUT_NEGATED` guard so an honest "the app is NOT up yet" / "I
  could not reach it" / "run curl to confirm" is never flagged. "works" is deliberately left to `_SUCCESS`;
  this net owns the up/serving/reachable/plumbed vocabulary `_SUCCESS` misses.
- **A health-check is the evidence.** `grounding.ran_healthcheck(command)` recognizes a TRUE liveness probe —
  curl / curl.exe / wget / Invoke-WebRequest / Invoke-RestMethod / nc / Test-NetConnection, or an
  `http://…` / `localhost:<port>` / `127.0.0.1:<port>` target. `agent.py` flips `ctx._runtime_ok = True` only
  when such a command RETURNS OK, exactly as it flips `ctx._verified_ok` for a passing CHECK. A
  connection-refused curl exits non-zero, so `_runtime_ok` stays False and the "serving" claim is flagged —
  the observed case. `docker ps` / `docker compose up` exit 0 regardless of whether the app serves, so they
  are deliberately NOT counted as liveness proof.
- **Runs in the deterministic `det` list.** `problems()` adds the net right beside `unverified_success_claim`,
  gated on `config.VERIFY_RUNTIME_DONE`, so it is model-free and never spawns a subagent. When the flag is
  off the net never runs and `_runtime_ok` is never set.
- **Pairs with the prompt rule.** specs/0051's HYGIENE note already tells the model not to claim a service is
  up unless it reached it; this spec is the deterministic backstop for when the model claims it anyway.

## Acceptance

Each item is an assertion in `scripts/check_runtime_done.py` (24/24, dep-free, no model).

- `ran_healthcheck` is True for curl / curl.exe / wget / Invoke-WebRequest / Test-NetConnection / an
  http/localhost:port target, and False for `docker ps`, `docker compose up`, `ls`, `echo`, `cat`.
- `unverified_runtime_claim` flags "Done — plumbing fixed", "Everything is plumbed", "the app is serving on
  :8080", "the server is up and running", "the container is now live and reachable" when unverified.
- It is CLEARED when `runtime_verified` is True, and NOT flagged for hedged/negated sentences ("NOT up yet",
  "run curl to confirm", "should be serving", "could not reach").
- `problems()` with the flag ON surfaces the runtime challenge for an unverified claim and stays silent for a
  verified one; with the flag OFF it never runs (byte-identical).
- `CODE_VERIFY_RUNTIME_DONE` defaults False when unset.

## Non-goals

- Not an enforced fix-until-up LOOP. This flags a false runtime claim (feeding the challenge back through the
  existing grounding re-prompt path); it does not itself retry docker. A true "keep fixing until the
  acceptance check passes" loop is the `pursue`/goal-loop's job (specs/0020) and is left as recorded debt.
- Not a grounding SPAWN retune. The observed per-turn verify-subagent spawns are a cost of a WORKING
  correctness net (the broad cited-path trigger catches honest-but-wrong claims); narrowing it risks
  weakening that catch, so it is deliberately NOT changed here. Operators who want the per-turn verifier
  cheaper can set `CODE_GROUNDING_EFFORT=low` (an existing knob) rather than have it inherit full effort.
  A live read-ledger for presence/absence claims is likewise left as recorded debt — invasive plumbing for a
  case the specs/0051 service-honesty rule + `absence_contradictions` already cover substantially.
- No `SCHEMA_VERSION` bump; the flag is not in `safety_fingerprint`.

## Byte-identity

`CODE_VERIFY_RUNTIME_DONE` off (default): `problems()` skips the net, `agent.py` never sets `_runtime_ok`, and
`ran_healthcheck` is never consulted — so grounding is byte-for-byte pre-0053. Verified: `check_grounding`,
`check_grounding_paths`, `check_completion_honesty` unchanged; `check_runtime_done` asserts the off-path
silence; full suite 50/50.
