"""
scripts/check_guardian.py

Acceptance harness for specs/0019 — the fail-CLOSED guardian, checked WITHOUT a model or a network (the
reviewer subagent is a stub). The crux is the inversion from grounding: any failure DENIES. Run:

    python scripts/check_guardian.py

Exits 0 only if every check holds — including that CODE_GUARDIAN off is byte-identical to today.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import config, guardian  # noqa: E402
from src.permissions import Permissions  # noqa: E402

_results = []


def check(label, cond):
    _results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


class _Ctx:
    def __init__(self, spawn=None, depth=0, interactive=False):
        self.cwd = ROOT
        self.spawn = spawn
        self.depth = depth
        self.interactive = interactive


def _spawn(verdict, calls, raises=False):
    def s(task, effort=None):
        calls.append(task)
        if raises:
            raise RuntimeError("reviewer exploded")
        return verdict
    return s


def main():
    # -- review(): APPROVE allows, everything else DENIES (fail-closed) ------------------------------
    check("review: a clean APPROVE -> allow", guardian.review("edit_file", "a.py", "ask", _Ctx(_spawn("APPROVE: safe", []))) is True)
    check("review: a DENY -> deny", guardian.review("edit_file", "a.py", "ask", _Ctx(_spawn("DENY: risky", []))) is False)
    check("review: NO spawn available -> DENY (fail-closed)", guardian.review("edit_file", "a.py", "ask", _Ctx(spawn=None)) is False)
    check("review: the reviewer RAISES -> DENY (fail-closed)",
          guardian.review("edit_file", "a.py", "ask", _Ctx(_spawn("APPROVE", [], raises=True))) is False)
    check("review: an EMPTY / subagent-error verdict -> DENY (fail-closed)",
          guardian.review("x", "y", "z", _Ctx(_spawn("", []))) is False
          and guardian.review("x", "y", "z", _Ctx(_spawn("(subagent error: boom)", []))) is False)

    # -- _parse_verdict: markdown tolerated; an AMBIGUOUS verdict denies -----------------------------
    check("_parse_verdict: **APPROVE** (markdown) -> True", guardian._parse_verdict("**APPROVE**: looks fine") is True)
    check("_parse_verdict: a bullet-wrapped DENY -> False", guardian._parse_verdict("- DENY: nope") is False)
    check("_parse_verdict: BOTH APPROVE and DENY present -> DENY (ambiguous, fail-closed)",
          guardian._parse_verdict("APPROVE the read but DENY the write") is False)
    check("_parse_verdict: prose with no verdict -> DENY", guardian._parse_verdict("I think it is probably fine") is False)

    # -- permissions integration: the guardian decides the ASK tier ---------------------------------
    _saved = config.GUARDIAN
    config.GUARDIAN = True
    p = Permissions("default", {"ask": ["edit_file(.env)"]}, [])
    calls = []
    d = p.decide("edit_file", {"path": ".env"}, _Ctx(_spawn("APPROVE: safe config edit", calls), depth=0))
    check("GUARDIAN on: an APPROVE lets an ask-tier call through headless (no human)", d.allowed and len(calls) == 1)
    check("GUARDIAN on: a DENY blocks the ask-tier call",
          not p.decide("edit_file", {"path": ".env"}, _Ctx(_spawn("DENY: no", []), depth=0)).allowed)

    # recursion gate: the reviewer's OWN ask-tier call (depth>0) never re-enters the guardian
    calls = []
    d = p.decide("edit_file", {"path": ".env"}, _Ctx(_spawn("APPROVE", calls), depth=1))
    check("recursion gate: at depth>0 the guardian is NOT consulted (headless -> denied, no spawn)",
          (not d.allowed) and calls == [])

    # the guardian governs the ASK tier ONLY — it can't turn a DENY-rule into an allow
    pdeny = Permissions("default", {"deny": ["edit_file(.env)"]}, [])
    check("GUARDIAN only touches ask: a DENY rule still blocks even with an APPROVE verdict",
          not pdeny.decide("edit_file", {"path": ".env"}, _Ctx(_spawn("APPROVE", []), depth=0)).allowed)

    # -- flag OFF -> byte-identical (guardian never consulted) ---------------------------------------
    config.GUARDIAN = False
    calls = []
    d = p.decide("edit_file", {"path": ".env"}, _Ctx(_spawn("APPROVE", calls), depth=0))
    check("GUARDIAN off: the reviewer is never consulted; a headless ask-tier call blocks (unchanged)",
          (not d.allowed) and calls == [])
    config.GUARDIAN = _saved

    passed, total = sum(_results), len(_results)
    print(f"\nVERDICT: {passed}/{total} {'[OK]' if passed == total else '[FAIL]'}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
