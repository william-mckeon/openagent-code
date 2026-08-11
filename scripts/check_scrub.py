"""
scripts/check_scrub.py

Acceptance harness for specs/0059 — trajectory PII/secret scrubbing (CODE_SCRUB_TRAJECTORY). Dep-free: no
model, no network. Proves the exact secret/PII classes from the live Centpilot run are redacted, that clean
content is untouched (no over-scrub), that scrub_record recurses without mutating the original, and that
trajectory._write scrubs the persisted file ONLY when the flag is on. Run:  python scripts/check_scrub.py
"""
import os
import sys
import json
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import config, scrub  # noqa: E402

_results = []


def check(label, cond):
    _results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


def main():
    # 1. SECRETS — the same CLASSES the run exposed, but FAKE fixtures (never commit real secrets/PII, which is
    #    the whole point of this feature). The patterns are identical to the real values' shapes.
    email = "test.user@example.com"
    r = scrub.scrub_text(f"user {email} logged in")
    check("email redacted", email not in r and "[redacted:email]" in r)
    r = scrub.scrub_text('window._CSRF = "FAKEcsrf0123456789abcdefghijklmnopqrstuv";')
    check("CSRF token value redacted, name kept", "FAKEcsrf0123" not in r and "_CSRF" in r and "[redacted:token]" in r)
    for tok, name in [("tgp_v1_FAKE0123456789abcdefghijklmnop", "together"),
                      ("sk-abcdefghijklmnopqrstuvwxyz012345", "openai"),
                      ("AKIAIOSFODNN7EXAMPLE", "aws"),
                      ("ghp_abcdefghijklmnopqrstuvwxyz0123456789", "github")]:
        r = scrub.scrub_text(f"key={tok} end")
        check(f"{name} key redacted", tok not in r and "[redacted:token]" in r)
    r = scrub.scrub_text("Authorization: Bearer abcdef0123456789ABCDEFxyz")
    check("bearer token redacted", "abcdef0123456789ABCDEFxyz" not in r and "[redacted:token]" in r)
    r = scrub.scrub_text("-----BEGIN RSA PRIVATE KEY-----\nMIIabc123XYZ\n-----END RSA PRIVATE KEY-----")
    check("private-key block redacted", "MIIabc123XYZ" not in r and "[redacted:private-key]" in r)

    # 2. FINANCIAL PII — the budget paste
    budget = "Paycheck 1$1,111.11Mortgage/Rent$2,222.22discover$3,333.33Balance $0.00"
    r = scrub.scrub_text(budget)
    check("dollar amounts redacted, labels kept",
          "1,111.11" not in r and "3,333.33" not in r and "2,222.22" not in r
          and "Paycheck" in r and "discover" in r and "[redacted:amount]" in r)
    r = scrub.scrub_text('"userId":"FAKEUSERID12345"')
    check("userId value redacted", "FAKEUSERID12345" not in r and "[redacted:id]" in r)
    r = scrub.scrub_text("card 4111 1111 1111 1111 on file")
    check("card number redacted", "4111 1111 1111 1111" not in r and "[redacted:card]" in r)

    # 3. clean content is UNTOUCHED (no over-scrub)
    for clean in ["def build(): return 42", "see src/main.go line 12", "docker-compose up --build",
                  "go 1.22", "the app responds 200 on localhost:8080", "step 5 [ok] run_command"]:
        check(f"clean text untouched: {clean!r}", scrub.scrub_text(clean) == clean)

    # 4. scrub_record recurses; keys left alone; non-strings pass; original not mutated
    rec = {"type": "tool_call", "email_field": email, "n": 7,
           "nested": {"content": f"paste {email}"}, "list": [email, "ok"], "flag": True}
    out = scrub.scrub_record(rec)
    check("scrub_record recurses, redacts VALUES, keeps keys + non-strings",
          "email_field" in out and email not in json.dumps(out)
          and out["n"] == 7 and out["type"] == "tool_call" and out["flag"] is True)
    check("scrub_record returns a COPY — original untouched", rec["email_field"] == email)

    # 5. idempotent
    once = scrub.scrub_text(budget)
    check("idempotent (re-scrubbing a marker is a no-op)", scrub.scrub_text(once) == once)

    # 6. trajectory._write integration: scrubs the persisted file ONLY when the flag is on
    from src.trajectory import Trajectory
    _saved = config.SCRUB_TRAJECTORY
    try:
        for flag in (True, False):
            config.SCRUB_TRAJECTORY = flag
            d = tempfile.mkdtemp(prefix="scrub-traj-")
            tj = Trajectory(d, task=f"my email {email}", model="x", cwd="/w", tool_schemas=[])
            tj.log_turn({"role": "user", "content": budget})
            tj.f.close()
            disk = open(tj.path, encoding="utf-8").read()
            if flag:
                check("trajectory ON: the persisted file has NO email / budget amounts (scrubbed)",
                      email not in disk and "1,111.11" not in disk and "[redacted:" in disk)
            else:
                check("trajectory OFF: the persisted file is VERBATIM (byte-identical)",
                      email in disk and "1,111.11" in disk and "[redacted:" not in disk)
    finally:
        config.SCRUB_TRAJECTORY = _saved

    # 7. flag is opt-in. specs/0077: the old check hardcoded "false" as the os.environ.get default and
    #    asserted _as_bool of it is False — a TAUTOLOGY that never touched config and would still pass if
    #    config.py flipped the default to "true". Assert the config SOURCE default literal is "false" instead.
    _cfg = open(os.path.join(ROOT, "src", "config.py"), encoding="utf-8").read()
    check("CODE_SCRUB_TRAJECTORY is opt-in — config.py defaults it to 'false' (not a tautology)",
          'SCRUB_TRAJECTORY = _as_bool(os.environ.get("CODE_SCRUB_TRAJECTORY", "false"))' in _cfg)

    passed, total = sum(_results), len(_results)
    print(f"\nVERDICT: {passed}/{total} {'[OK]' if passed == total else '[FAIL]'}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
