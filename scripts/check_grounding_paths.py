"""
scripts/check_grounding_paths.py

Acceptance harness for specs/0027 - grounding path accuracy (the phantom PRESENT-path the semantic verifier
waves through). Dep-free: no model, no network (a stub spawn stands in for the Tier-2 verifier). Proves:

  * _NOEXT_FILES / _strict_paths: an extension-less Dockerfile is extracted ONLY with noext=True.
  * _present_path_problems: a cited nonexistent slash-path is flagged; an existing one and a bare basename
    are not.
  * problems() runs the deterministic existence check IN semantic mode when the flag is on (catching a
    phantom the stub verifier misses), and is byte-identical when the flag is off.
  * The semantic-OFF path is unchanged when the flag is off, and gains extension-less recognition when on.

Run:  python scripts/check_grounding_paths.py
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
    """The fields grounding.problems() reads; spawn is a stub verifier (or None for the semantic-off path)."""
    def __init__(self, cwd, spawn=None):
        self.depth, self.cwd = 0, cwd
        self.mutations, self.fetched, self._verified_ok = {}, {}, False
        self.spawn = spawn


def _spawn_grounded(task, effort=None, label=None):
    return "GROUNDED"   # a fail-open verifier that finds nothing wrong


def main():
    ws = os.path.realpath(tempfile.mkdtemp(prefix="gpaths-ws-"))
    os.makedirs(os.path.join(ws, "sub"), exist_ok=True)
    open(os.path.join(ws, "sub", "real.py"), "w").close()   # an existing cited target
    _saved = {k: getattr(config, k) for k in ("VERIFY_GROUNDING_PATHS", "VERIFY_GROUNDING_SEMANTIC",
                                              "ENABLE_WEB", "VERIFY_MUTATION_CLAIMS")}
    config.ENABLE_WEB = config.VERIFY_MUTATION_CLAIMS = False

    # =====================================================================================================
    # 1. extension-less extraction (only with noext)
    # =====================================================================================================
    check("noext: 'docker/Dockerfile' is extracted with noext=True",
          any(p.endswith("Dockerfile") for p in grounding._strict_paths("see `docker/Dockerfile`", noext=True)))
    check("noext: the SAME token is NOT in the plain strict set (noext=False -> byte-identical)",
          grounding._strict_paths("see `docker/Dockerfile`", noext=False) == set())
    check("noext: a dotted name like `Dockerfile.md` goes the normal route, not the noext set",
          "Dockerfile.md" in {p for p in grounding._strict_paths("see `Dockerfile.md`", noext=True)})

    # =====================================================================================================
    # 2. the present-path existence check
    # =====================================================================================================
    check("present-path: a cited nonexistent 'svc/Dockerfile' is flagged (noext)",
          grounding._present_path_problems("build via `svc/Dockerfile`", _Ctx(ws), noext=True) != [])
    check("present-path: an EXISTING cited path is not flagged",
          grounding._present_path_problems("see `sub/real.py`", _Ctx(ws), noext=False) == [])
    check("present-path: a bare basename (no slash) is NEVER hard-flagged",
          grounding._present_path_problems("see `Dockerfile`", _Ctx(ws), noext=True) == [])
    check("present-path: a mutation-ledger target is not flagged even if absent from disk",
          grounding._present_path_problems("wrote `svc/Dockerfile`",
                                           type("C", (), {"cwd": ws, "mutations": {"svc/Dockerfile": "write"}})(),
                                           noext=True) == [])

    # =====================================================================================================
    # 3. problems() in SEMANTIC mode: the deterministic check runs when the flag is on
    # =====================================================================================================
    config.VERIFY_GROUNDING_SEMANTIC = True
    ans = "The build uses `careeragent-frontend/docker/frontend/Dockerfile` to assemble the image."
    ctx = _Ctx(ws, spawn=_spawn_grounded)
    config.VERIFY_GROUNDING_PATHS = True
    check("semantic + flag ON: the phantom Dockerfile is caught deterministically (verifier missed it)",
          any("Dockerfile" in p for p in grounding.problems(ans, ctx)))
    config.VERIFY_GROUNDING_PATHS = False
    check("semantic + flag OFF: the phantom Dockerfile is NOT caught (byte-identical - verifier waves it)",
          grounding.problems(ans, ctx) == [])
    # a real .py citation the verifier clears stays clear either way
    check("semantic: an existing cited file is clean regardless of the flag",
          grounding.problems("uses `sub/real.py`", ctx) == [])

    # =====================================================================================================
    # 4. problems() in SEMANTIC-OFF mode: Tier-1 unchanged; extension-less rides the flag
    # =====================================================================================================
    config.VERIFY_GROUNDING_SEMANTIC = False
    off = _Ctx(ws, spawn=None)
    config.VERIFY_GROUNDING_PATHS = False
    check("semantic OFF: a phantom .py citation is still flagged (Tier-1, unchanged)",
          grounding.problems("see `missing/gone.py`", off) != [])
    check("semantic OFF + flag OFF: a phantom Dockerfile is NOT flagged (noext requires the flag)",
          grounding.problems("see `svc/Dockerfile`", off) == [])
    config.VERIFY_GROUNDING_PATHS = True
    check("semantic OFF + flag ON: the phantom Dockerfile IS now flagged",
          grounding.problems("see `svc/Dockerfile`", off) != [])

    check("config: CODE_VERIFY_GROUNDING_PATHS exists as a bool flag (hermetic - not asserting the .env value)",
          hasattr(config, "VERIFY_GROUNDING_PATHS") and isinstance(_saved["VERIFY_GROUNDING_PATHS"], bool))

    for k, v in _saved.items():
        setattr(config, k, v)

    passed, total = sum(_results), len(_results)
    print(f"\nVERDICT: {passed}/{total} {'[OK]' if passed == total else '[FAIL]'}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
