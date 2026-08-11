"""
scripts/check_gatehonesty_0073.py

Acceptance harness for specs/0073 — six gate-honesty / corpus-poison fixes from the bug hunt. Dep-free (no
model, no network): exercises grounding's check/healthcheck classifiers and backslash citations, the scrub
patterns, and verify_edits' timeout handling directly. Run:

    python scripts/check_gatehonesty_0073.py
"""
import os
import sys
import subprocess
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import grounding as g, scrub, verify_edits, config   # noqa: E402

_results = []


def check(label, cond):
    _results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


def main():
    # 1. ran_check no longer flips _verified_ok on a non-check command
    check("ran_check: 'mkdir build' / 'git checkout build' / 'cat lint.log' / 'npm run dev' are NOT checks",
          not any(g.ran_check(c) for c in
                  ["mkdir build", "git checkout build", "cat lint.log", "npm run dev",
                   "ls build", "Remove-Item build -Recurse", "echo build done"]))
    check("ran_check: real checks still count (pytest / npm run build / go build / tsc / make test / eslint)",
          all(g.ran_check(c) for c in
              ["pytest -q", "npm run build", "go build ./...", "tsc --noEmit", "make test",
               "eslint .", "cargo test", "dotnet test", "cmake --build .", "npm test"]))

    # 2. ran_healthcheck: a bare URL is not proof of liveness
    check("ran_healthcheck: 'git clone https://...' / 'pip install -i https://...' are NOT health checks",
          not g.ran_healthcheck("git clone https://github.com/x/y")
          and not g.ran_healthcheck("pip install -i https://pypi.org/simple foo"))
    check("ran_healthcheck: an actual probe tool still counts (curl / iwr / Test-NetConnection)",
          g.ran_healthcheck("curl http://localhost:8080/health") and g.ran_healthcheck("iwr http://localhost:8080")
          and g.ran_healthcheck("Test-NetConnection -Port 8080"))

    # 3. Windows backslash citations are no longer invisible to the grounding path checks
    check("cited_paths: a backtick Windows path `src\\main.py` is now seen (was invisible)",
          "src/main.py" in g.cited_paths(r"edited `src\main.py` today", strict=True))
    check("cited_paths: a forward-slash citation still works (no regression)",
          "src/main.py" in g.cited_paths("edited `src/main.py`", strict=True))

    # 4. scrub the UNQUOTED .env / YAML / export secret form
    secrets = {"OPENAI_API_KEY=zk9v2x8q4m7n3b6c5d1f0g2h4": "zk9v2x8q4m7n3b6c5d1f0g2h4",
               "password: hunter2secret99": "hunter2secret99",
               "export API_KEY=abc123def456ghi": "abc123def456ghi"}
    check("scrub: UNQUOTED secrets are redacted (value gone, name kept)",
          all("[redacted:token]" in scrub.scrub_text(t) and v not in scrub.scrub_text(t)
              for t, v in secrets.items()))
    check("scrub: the quoted form still redacts; prose is left alone (no over-scrub)",
          "[redacted:token]" in scrub.scrub_text('api_key="zk9v2x8q4m7n3b6c5d"')
          and scrub.scrub_text("password: use a strong one") == "password: use a strong one")

    # 5. scrub card: grouped-only, so an epoch / id does not false-match
    check("scrub: a GROUPED 4-4-4-4 card is redacted; a solid 16-digit epoch/id is NOT",
          "[redacted:card]" in scrub.scrub_text("card 4111 1111 1111 1111 on file")
          and "[redacted:card]" not in scrub.scrub_text("ts=1699564800000000 count=1234567890123456"))

    # 6. verify_edits: a TIMEOUT is a FAILED check, never a passing verification reward
    _orig = subprocess.run

    def _timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd=(a[0] if a else "x"), timeout=config.VERIFY_TIMEOUT)
    subprocess.run = _timeout
    try:
        ok, out = verify_edits._default_run_fn(tempfile.mkdtemp())(["pytest", "-q"])
    finally:
        subprocess.run = _orig
    check("verify_edits: a verifier TIMEOUT returns (False, ...) — a failed check, never a pass",
          ok is False and "timed out" in out)

    passed, total = sum(_results), len(_results)
    print(f"\nVERDICT: {passed}/{total} {'[OK]' if passed == total else '[FAIL]'}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
