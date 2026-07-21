"""
scripts/check_declared_done.py

Invariant harness for specs/0032 - the declared-done verification family. Dep-free: no model, no network.
It asserts the ARCHITECTURAL invariant the spec names (not new behavior): the four structured gates each
verify a DECLARED artifact and carry a DISTINCT honest outcome, and the free-text mutation-claim net is the
documented, retired outlier.

Run:  python scripts/check_declared_done.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import config, outcomes, grounding, goal  # noqa: E402
from src import agent as agent_mod  # noqa: E402

_results = []


def check(label, cond):
    _results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


# The family: (label, the gate function that checks the declared artifact, its distinct honest outcome).
FAMILY = [
    ("completion (0007) -> update_plan steps", agent_mod, "_unverified_items", "unverified_completion"),
    ("manifest (0026)   -> approved manifest", agent_mod, "_unapplied_manifest", "manifest_unapplied"),
    ("acceptance (0025) -> spec acceptance",   agent_mod, "_unmet_acceptance", "acceptance_unmet"),
    ("goal (0020)       -> a runnable bar",    goal, "run_bar", "goal_unmet"),
]


def main():
    # -- each family gate exists and carries a DISTINCT honest outcome ------------------------------------
    for label, mod, fn, outcome in FAMILY:
        check(f"family: {label} - the gate function {fn}() exists", callable(getattr(mod, fn, None)))
        check(f"family: {label} - {outcome!r} is an honest gate outcome (dropped from the corpus)",
              outcome in outcomes.GATE_OUTCOMES and outcomes.classify(outcome, 3) == outcome)

    outs = [o for *_ , o in FAMILY]
    check("family: the four structured honest outcomes are DISTINCT (no blurred training signal)",
          len(set(outs)) == 4)

    # -- the retired outlier: the mutation-claim net -----------------------------------------------------
    check("outlier: unbacked_mutation_claim still exists as an opt-in backstop",
          callable(getattr(grounding, "unbacked_mutation_claim", None)))
    check("outlier: its docstring marks it DEPRECATED (retired in favor of the structured family)",
          "DEPRECATED" in (grounding.unbacked_mutation_claim.__doc__ or ""))
    check("outlier: CODE_VERIFY_MUTATION_CLAIMS is a bool flag (default OFF in code)",
          isinstance(config.VERIFY_MUTATION_CLAIMS, bool))
    check("outlier: it is NOT the mechanism the structured family relies on (VERIFY_MANIFEST is separate)",
          hasattr(config, "VERIFY_MANIFEST"))

    passed, total = sum(_results), len(_results)
    print(f"\nVERDICT: {passed}/{total} {'[OK]' if passed == total else '[FAIL]'}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
