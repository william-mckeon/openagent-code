"""
scripts/check_resume_0074.py

Acceptance harness for specs/0074 — six resume-integrity fixes from the bug hunt. Dep-free: session.py's
model/litellm imports are lazy, so its pure helpers import clean. Run:

    python scripts/check_resume_0074.py
"""
import os
import sys
import json
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src.context import sanitize_tail          # noqa: E402
from src import session                        # noqa: E402
from src.planner import JsonPlanner            # noqa: E402
from src.permissions import Permissions        # noqa: E402

_results = []


def check(label, cond):
    _results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


def U(t):
    return {"role": "user", "content": t}


def A(*ids):
    return {"role": "assistant", "content": "", "tool_calls": [{"id": i} for i in ids]}


def T(cid):
    return {"role": "tool", "tool_call_id": cid, "content": "r"}


def main():
    # 1. sanitize_tail — the full-history pairing scan (specs/0074), not just the tail
    mid = sanitize_tail([U("q1"), A("1"), U("q2"), U("q3")])
    check("sanitize_tail: a MID-list dangling tool_call is STUBBED (pairing valid) and later turns survive",
          any(m.get("role") == "tool" and m.get("tool_call_id") == "1" for m in mid) and mid[-1] == U("q3"))
    part_mid = sanitize_tail([U("q"), A("A", "B"), T("A"), U("q2")])
    tids = [m.get("tool_call_id") for m in part_mid if m.get("role") == "tool"]
    check("sanitize_tail: a PARTIAL-results mid group stubs the missing id (A real + B stubbed), turns kept",
          "A" in tids and "B" in tids and U("q2") in part_mid)
    check("sanitize_tail: a TRAILING partial group is dropped whole (nothing follows to keep)",
          sanitize_tail([U("q"), A("A", "B"), T("A")]) == [U("q")])

    # regression: the original specs/0034 cases still hold
    check("sanitize_tail regression: clean pairing is a no-op",
          sanitize_tail([U("q"), A("1"), T("1")]) == [U("q"), A("1"), T("1")])
    check("sanitize_tail regression: a trailing dangle is dropped; a leading orphan is dropped",
          sanitize_tail([U("q"), A("1")]) == [U("q")] and sanitize_tail([T("1"), U("q")]) == [U("q")])

    # 2. _working_from filters ALL role:'system' turns (not just a leading one)
    recs = [{"type": "turn", "message": {"role": "system", "content": "PROMPT"}},
            {"type": "turn", "message": U("hi")},
            {"type": "turn", "message": {"role": "system", "content": "ENV cwd=x date=2026-01-01"}},
            {"type": "turn", "message": {"role": "assistant", "content": "ok"}}]
    w = session._working_from(recs)
    check("resume: _working_from strips ALL system turns (stale per-turn env blocks never re-sent)",
          all(m.get("role") != "system" for m in w) and len(w) == 2)

    # 3. _load_records skips a corrupt/truncated final line instead of crashing
    f = os.path.join(tempfile.mkdtemp(prefix="res74_"), "s.jsonl")
    with open(f, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "turn", "message": U("hi")}) + "\n")
        fh.write("{bad truncated json")          # a process killed mid-write
    recs2 = session._load_records(f)
    check("resume: _load_records skips a corrupt/truncated line, keeps the good records (no crash)",
          len(recs2) == 1 and recs2[0]["type"] == "turn")

    # 4. _replay_dir_grants restores mid-session grants BY TIER
    p = Permissions("bypass", {}, [])
    session._replay_dir_grants(
        [{"type": "dir_grant", "path": "D:/ref", "tier": "read_only"},
         {"type": "dir_grant", "path": "E:/rw", "tier": "extra"}], p)
    check("resume: _replay_dir_grants restores a read_only grant and an extra grant to the right bucket",
          "D:/ref" in p.read_only_roots and "E:/rw" in p.extra_roots and "D:/ref" not in p.extra_roots)
    p2 = Permissions("bypass", {}, [])
    session._replay_dir_grants([{"type": "turn"}, {"type": "model_call"}], p2)   # no dir_grant records
    check("resume: no dir_grant records -> nothing replayed (byte-identical for old trajectories)",
          not p2.read_only_roots and not p2.extra_roots)

    # 5. JsonPlanner.reset clears the per-task nudge budget (NativePlanner has no reset)
    jp = JsonPlanner(None)
    jp.nudges = 3
    jp.reset()
    from src.planner import NativePlanner
    check("planner: JsonPlanner.reset() clears nudges; NativePlanner has no reset() (per-task, no leak)",
          jp.nudges == 0 and not hasattr(NativePlanner, "reset"))

    passed, total = sum(_results), len(_results)
    print(f"\nVERDICT: {passed}/{total} {'[OK]' if passed == total else '[FAIL]'}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
