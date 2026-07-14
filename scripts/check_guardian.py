"""
scripts/check_guardian.py

Acceptance harness for specs/0019 — the fail-CLOSED guardian, checked WITHOUT a model or a network (the
reviewer subagent is a stub). The crux is the inversion from grounding: any failure DENIES. It fires ONLY
headless (a human present gets the [y/N] prompt), reviews an identical (tool, target) once per turn, and
surfaces a reason. Run:

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
    # Mirrors the real ctx.spawn signature (task, effort=None, label=None) so a label= call doesn't blow up.
    def s(task, effort=None, label=None):
        calls.append(label or task)
        if raises:
            raise RuntimeError("reviewer exploded")
        return verdict
    return s


def main():
    # -- review(): APPROVE allows, everything else DENIES (fail-closed); a Verdict carries a reason -------
    v = guardian.review("edit_file", "a.py", "ask", _Ctx(_spawn("APPROVE: safe config edit", [])))
    check("review: a clean APPROVE -> approved, with the reason carried", v.approved is True and "safe" in v.reason)
    check("review: a DENY -> deny", guardian.review("edit_file", "a.py", "ask", _Ctx(_spawn("DENY: risky", []))).approved is False)
    check("review: NO spawn available -> DENY (fail-closed)", guardian.review("edit_file", "a.py", "ask", _Ctx(spawn=None)).approved is False)
    check("review: the reviewer RAISES -> DENY (fail-closed)",
          guardian.review("edit_file", "a.py", "ask", _Ctx(_spawn("APPROVE", [], raises=True))).approved is False)
    check("review: an EMPTY / subagent-error verdict -> DENY (fail-closed)",
          guardian.review("x", "y", "z", _Ctx(_spawn("", []))).approved is False
          and guardian.review("x", "y", "z", _Ctx(_spawn("(subagent error: boom)", []))).approved is False)

    # -- _parse_verdict: markdown tolerated; an AMBIGUOUS verdict denies; a reason is extracted -----------
    check("_parse_verdict: **APPROVE** (markdown) -> True", guardian._parse_verdict("**APPROVE**: looks fine").approved is True)
    check("_parse_verdict: a bullet-wrapped DENY -> False", guardian._parse_verdict("- DENY: nope").approved is False)
    check("_parse_verdict: BOTH APPROVE and DENY present -> DENY (ambiguous, fail-closed)",
          guardian._parse_verdict("APPROVE the read but DENY the write").approved is False)
    check("_parse_verdict: prose with no verdict -> DENY", guardian._parse_verdict("I think it is probably fine").approved is False)
    check("_parse_verdict: reason is the tail after the verdict word",
          guardian._parse_verdict("APPROVE: routine npm install").reason == "routine npm install")

    # -- calibration: a routine in-workspace install is described as APPROVE-able in the reviewer prompt --
    _prompt = guardian._review_task("run_command", "cd src/homepage && npm install", "acceptEdits mode")
    check("prompt: routine installs (npm/pip/go) are called out as safe to APPROVE",
          "npm install" in _prompt and "APPROVE" in _prompt and "arbitrary network" in _prompt.lower())

    # -- permissions integration: the guardian decides the ASK tier, HEADLESS ----------------------------
    _saved = config.GUARDIAN
    config.GUARDIAN = True
    p = Permissions("default", {"ask": ["edit_file(.env)"]}, [])
    calls = []
    d = p.decide("edit_file", {"path": ".env"}, _Ctx(_spawn("APPROVE: safe config edit", calls), depth=0))
    check("GUARDIAN on + headless: an APPROVE lets an ask-tier call through", d.allowed and len(calls) == 1)
    check("GUARDIAN on + headless: the decision reason carries the guardian's verdict text",
          "guardian approved" in d.reason and "safe" in d.reason)
    check("GUARDIAN on + headless: a DENY blocks the ask-tier call",
          not p.decide("edit_file", {"path": ".env"}, _Ctx(_spawn("DENY: no", []), depth=0)).allowed)

    # headless-only: when a human IS present, the guardian returns None (falls through to the [y/N]
    # prompt). Probe _guardian directly so the test never blocks on the real input() prompt.
    calls = []
    tgt = p._target("edit_file", {"path": ".env"}, _Ctx())
    g_int = p._guardian("edit_file", tgt, "ask rule", _Ctx(_spawn("APPROVE", calls), depth=0, interactive=True))
    check("headless-only: interactive -> guardian returns None (not consulted), no spawn",
          g_int is None and calls == [])

    # per-turn cache: the SAME (tool, target) is reviewed once, even across two decide() calls
    calls = []
    ctx_cached = _Ctx(_spawn("APPROVE: ok", calls), depth=0)
    a1 = p.decide("edit_file", {"path": ".env"}, ctx_cached)
    a2 = p.decide("edit_file", {"path": ".env"}, ctx_cached)
    check("cache: an identical ask-tier call is reviewed ONCE per turn (spawn called once)",
          a1.allowed and a2.allowed and len(calls) == 1)

    # recursion gate: the reviewer's OWN ask-tier call (depth>0) never re-enters the guardian
    calls = []
    p.decide("edit_file", {"path": ".env"}, _Ctx(_spawn("APPROVE", calls), depth=1))
    check("recursion gate: at depth>0 the guardian is NOT consulted (no spawn)", calls == [])

    # the guardian governs the ASK tier ONLY — it can't turn a DENY-rule into an allow
    pdeny = Permissions("default", {"deny": ["edit_file(.env)"]}, [])
    check("GUARDIAN only touches ask: a DENY rule still blocks even with an APPROVE verdict",
          not pdeny.decide("edit_file", {"path": ".env"}, _Ctx(_spawn("APPROVE", []), depth=0)).allowed)

    # -- flag OFF -> byte-identical (guardian never consulted) --------------------------------------------
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
