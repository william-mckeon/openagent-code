"""
scripts/check_verify_reward.py

Acceptance harness for specs/0014 sub-phase C — the objective reward label + honest corpus drop, checked
WITHOUT a model or a network. Proves: a genuinely-failing run is dropped from SFT, a failed-then-FIXED
run (which logs only the passing final result) is KEPT, and the trajectory writes a readable reward
record. Run:

    python scripts/check_verify_reward.py

Exits 0 only if every check holds.
"""
import os
import sys
import json
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from train.convert import is_trainable  # noqa: E402
from src.trajectory import Trajectory  # noqa: E402

_results = []


def check(label, cond):
    _results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


def _sess(outcome, verifs=()):
    recs = [{"type": "session_start", "schema_version": "0.6.0", "session_id": "s", "task": "x", "model": "m"}]
    for ok in verifs:
        recs.append({"type": "verification", "command": "python -m py_compile a.py", "ok": ok, "output": ""})
    recs.append({"type": "session_end", "outcome": outcome, "tool_calls": 3})
    return recs


def main():
    # 1. a genuinely-failing verify run is dropped from SFT (KEEP_OUTCOMES auto-drops the outcome)
    k1, r1 = is_trainable(_sess("verify_failed_edits"))
    check("convert DROPS a verify_failed_edits run (never distill code that fails its check)",
          (not k1) and r1 == "verify_failed_edits")

    # 2. a 'completed' run whose verification record shows a FAILURE is also dropped - which is exactly
    #    why the gate logs ONLY the final (passing) result for a fixed run, not the intermediate fail.
    k2, _ = is_trainable(_sess("completed", verifs=(False,)))
    check("convert DROPS a 'completed' run whose verification record shows a FAILURE", not k2)

    # 3. a failed-then-FIXED run ends 'completed' and logs only the PASSING result -> KEPT
    k3, _ = is_trainable(_sess("completed", verifs=(True,)))
    check("convert KEEPS a 'completed' run whose verification PASSED (failed-then-fixed stays trainable)", k3)

    # 4. a real Trajectory writes a readable verification reward record
    d = tempfile.mkdtemp(prefix="verifyrew_")
    t = Trajectory(d, "x", "m", d, tool_schemas=[])
    t.log_verification("python -m py_compile a.py", False, "a.py:1: SyntaxError: bad token")
    t.f.flush()
    recs = [json.loads(ln) for ln in open(t.path, encoding="utf-8") if ln.strip()]
    v = next((r for r in recs if r.get("type") == "verification"), None)
    check("Trajectory.log_verification writes a verification reward record (command/ok/output)",
          v is not None and v.get("command") == "python -m py_compile a.py"
          and v.get("ok") is False and "SyntaxError" in v.get("output", ""))

    passed, total = sum(_results), len(_results)
    print(f"\nVERDICT: {passed}/{total} {'[OK]' if passed == total else '[FAIL]'}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
