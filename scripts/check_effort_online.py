"""
scripts/check_effort_online.py

Acceptance harness for the OPT-IN online effort learner (specs/0021), checked WITHOUT a model or network.
The learner is deterministic given a fixed history, so its decisions and persisted state are fully
pinnable. Run:

    python scripts/check_effort_online.py
"""
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import config, effort, effort_online  # noqa: E402

_results = []


def check(label, cond):
    _results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


def main():
    _saved = (config.EFFORT_POLICY, config.EFFORT_STATE, config.EFFORT_THRESHOLD,
              config.EFFORT_FLOOR, config.EFFORT_MAX)
    config.EFFORT_THRESHOLD, config.EFFORT_FLOOR, config.EFFORT_MAX = 2, "medium", "high"
    config.EFFORT_STATE = ""   # in-memory unless a test sets a path

    SIG = "refactor the auth module across several files"
    OTHER = "list the files in docs"

    # -- isolation: it's only imported when SELECTED (default path stays deterministic) -----------------
    config.EFFORT_POLICY = "reactive"
    check("default policy does NOT load the online learner", effort.load_policy().name == "reactive")
    config.EFFORT_POLICY = "online"
    check("CODE_EFFORT_POLICY=online loads the learner", effort.load_policy().name == "online")

    # -- a fresh learner behaves like reactive (struggle/request only) ----------------------------------
    op = effort_online.OnlinePolicy()
    check("fresh: no struggle, no history -> stays at floor", op.decide("medium", None, 0, "high", SIG) == "medium")
    check("fresh: still auto-escalates on struggle (reactive behavior underneath)",
          op.decide("medium", None, 2, "high", SIG) == "high")

    # -- it LEARNS: escalations that SUCCEEDED on a signature make it pre-escalate that signature --------
    op.update(SIG, escalated=True, success=True)
    check("one observation is not enough to act (needs >= 2)", op.decide("medium", None, 0, "high", SIG) == "medium")
    op.update(SIG, escalated=True, success=True)
    check("after 2 successful escalations it PRE-escalates the signature (no struggle needed)",
          op.decide("medium", None, 0, "high", SIG) == "high")
    check("a DIFFERENT signature is unaffected (learning is per-signature)",
          op.decide("medium", None, 0, "high", OTHER) == "medium")

    # -- it does NOT learn from a non-escalated turn, and win-RATE gates it ------------------------------
    op2 = effort_online.OnlinePolicy()
    op2.update(SIG, escalated=False, success=True)   # not an escalated turn -> teaches nothing
    check("a non-escalated outcome is ignored", op2.decide("medium", None, 0, "high", SIG) == "medium")
    op3 = effort_online.OnlinePolicy()
    op3.update(SIG, escalated=True, success=False)
    op3.update(SIG, escalated=True, success=False)   # escalating did NOT help -> don't pre-escalate
    check("escalations that FAILED do not trigger pre-escalation (win-rate gate)",
          op3.decide("medium", None, 0, "high", SIG) == "medium")

    # -- persistence: state survives a reload; a corrupt/missing file never raises ----------------------
    d = tempfile.mkdtemp(prefix="effstate_")
    config.EFFORT_STATE = os.path.join(d, "eff.json")
    a = effort_online.OnlinePolicy()
    a.update(SIG, True, True); a.update(SIG, True, True)
    b = effort_online.OnlinePolicy()   # a fresh instance reads the persisted stats
    check("learned state persists to disk and is reloaded", b.decide("medium", None, 0, "high", SIG) == "high")
    with open(config.EFFORT_STATE, "w", encoding="utf-8") as f:
        f.write("{ not json")
    check("a corrupt state file starts empty, never raises",
          effort_online.OnlinePolicy().decide("medium", None, 0, "high", SIG) == "medium")
    config.EFFORT_STATE = os.path.join(d, "missing.json")
    check("a missing state file starts empty, never raises",
          effort_online.OnlinePolicy().decide("medium", None, 0, "high", SIG) == "medium")

    # -- the learner still respects the cap and escalate-only -------------------------------------------
    config.EFFORT_STATE = ""
    op4 = effort_online.OnlinePolicy()
    op4.update(SIG, True, True); op4.update(SIG, True, True)
    check("a learned pre-escalation is still CAPPED", op4.decide("medium", None, 0, "medium", SIG) == "medium")

    # -- signature: similar tasks share stats, dissimilar don't ----------------------------------------
    check("signature buckets similar tasks together (both 'refactor')",
          effort_online._sig("refactor module A") == effort_online._sig("refactor module A slightly differently")
          or effort_online._sig("refactor x").split("#")[0] == effort_online._sig("refactor y").split("#")[0])

    config.EFFORT_POLICY, config.EFFORT_STATE, config.EFFORT_THRESHOLD, config.EFFORT_FLOOR, config.EFFORT_MAX = _saved

    passed, total = sum(_results), len(_results)
    print(f"\nVERDICT: {passed}/{total} {'[OK]' if passed == total else '[FAIL]'}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
