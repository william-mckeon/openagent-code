"""
scripts/check_runtime_done.py

Acceptance harness for specs/0053 — the runtime-done honesty gate (CODE_VERIFY_RUNTIME_DONE). Dep-free: no
model, no network. Proves a "the service is up / serving / plumbed" claim is FLAGGED when no health-check
reached it this turn, CLEARED when one did, hedge/negation-safe, and byte-identical when the flag is off.

Run:  python scripts/check_runtime_done.py
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import config, grounding  # noqa: E402

_results = []


def check(label, cond):
    _results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


class _Ctx:
    """Minimal ctx for grounding.problems() — spawn=None isolates the deterministic nets (no semantic verifier)."""
    def __init__(self, cwd, runtime_ok=False, verified_ok=False):
        self.depth = 0
        self.cwd = cwd
        self._verified_ok = verified_ok
        self._runtime_ok = runtime_ok
        self.fetched = {}
        self.mutations = {}
        self.spawn = None


def main():
    ws = os.path.realpath(tempfile.mkdtemp(prefix="runtime-ws-"))
    keys = ("VERIFY_RUNTIME_DONE", "VERIFY_GROUNDING_SEMANTIC", "VERIFY_MUTATION_CLAIMS",
            "GROUND_SKIP_GREENFIELD", "VERIFY_GROUNDING_PATHS")
    saved = {k: getattr(config, k) for k in keys}
    try:
        config.VERIFY_MUTATION_CLAIMS = False
        config.GROUND_SKIP_GREENFIELD = False
        config.VERIFY_GROUNDING_PATHS = False
        config.VERIFY_GROUNDING_SEMANTIC = False

        # 1. ran_healthcheck: true HTTP/port probes ARE health checks; docker ps / ls / echo are NOT
        for cmd in ("curl localhost:8080", "curl.exe -s http://127.0.0.1:8080/", "wget http://x/health",
                    "Invoke-WebRequest http://localhost:3000", "Test-NetConnection -Port 80 localhost"):
            check(f"ran_healthcheck TRUE: {cmd!r}", grounding.ran_healthcheck(cmd))
        for cmd in ("docker ps", "docker compose up -d", "ls deploy/", "echo hi", "cat app.js"):
            check(f"ran_healthcheck FALSE: {cmd!r}", not grounding.ran_healthcheck(cmd))

        # 2. unverified_runtime_claim: a runtime-up claim with no successful probe is flagged
        for c in ["Done - plumbing fixed.", "Everything is plumbed.", "The app is serving on port 8080.",
                  "The server is up and running.", "The container is now live and reachable."]:
            check(f"claim flagged when unverified: {c!r}", len(grounding.unverified_runtime_claim(c, False)) == 1)
        # verified -> cleared
        check("a runtime claim is CLEARED when a health-check confirmed it",
              grounding.unverified_runtime_claim("The app is serving.", True) == [])
        # hedged / negated -> not flagged
        for c in ["The app is NOT up yet.", "Run curl to confirm it is serving.",
                  "It should be serving once you start it.", "I could not reach the server."]:
            check(f"hedged/negated NOT flagged: {c!r}", grounding.unverified_runtime_claim(c, False) == [])

        # 3. problems() integration — flag ON: the runtime message appears; flag OFF: byte-identical (absent)
        text = "The server is up and serving on port 8080."
        config.VERIFY_RUNTIME_DONE = True
        probs_on = grounding.problems(text, _Ctx(ws, runtime_ok=False))
        check("problems() ON + unverified runtime claim -> flagged", any("did not observe" in p for p in probs_on))
        probs_ok = grounding.problems(text, _Ctx(ws, runtime_ok=True))
        check("problems() ON + verified runtime -> not flagged", not any("did not observe" in p for p in probs_ok))
        config.VERIFY_RUNTIME_DONE = False
        probs_off = grounding.problems(text, _Ctx(ws, runtime_ok=False))
        check("problems() OFF -> runtime net never runs (byte-identical)",
              not any("did not observe" in p for p in probs_off))

        # 4. the flag is opt-in, tested against the fallback independent of this repo's own .env
        _s = os.environ.pop("CODE_VERIFY_RUNTIME_DONE", None)
        default_off = config._as_bool(os.environ.get("CODE_VERIFY_RUNTIME_DONE", "false")) is False
        if _s is not None:
            os.environ["CODE_VERIFY_RUNTIME_DONE"] = _s
        check("CODE_VERIFY_RUNTIME_DONE defaults False when unset (opt-in)", default_off)
    finally:
        for k, v in saved.items():
            setattr(config, k, v)

    passed, total = sum(_results), len(_results)
    print(f"\nVERDICT: {passed}/{total} {'[OK]' if passed == total else '[FAIL]'}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
