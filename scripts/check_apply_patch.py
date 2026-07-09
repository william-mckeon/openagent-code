"""
scripts/check_apply_patch.py

Acceptance harness for specs/0013 sub-phase B — the atomic, grammar-validated apply_patch tool, checked
WITHOUT a model or a network. apply_patch is exercised on temp workspaces; the toolset gate is toggled
on the config module. Run:

    python scripts/check_apply_patch.py

Exits 0 only if every check holds — including ATOMICITY (one bad op touches zero files).
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import config, patch  # noqa: E402
from src.tools import Context  # noqa: E402
from src.toolset import active_tools  # noqa: E402

_results = []


def check(label, cond):
    _results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


def _w(d, name, content):
    p = os.path.join(d, name)
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(content)
    return p


def _r(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def _env(*lines):
    return "\n".join(lines) + "\n"


def main():
    # -- 1. a valid multi-op patch: Add / Update / Delete / Move all land ------
    d = tempfile.mkdtemp(prefix="applypatch_")
    _w(d, "upd.py", "def f():\n    return 1\n")
    _w(d, "del.py", "obsolete\n")
    _w(d, "mov.py", "movable\n")
    r = patch.apply_patch({"patch": _env(
        "*** Begin Patch",
        "*** Add File: new.py", "+def g():", "+    return 2",
        "*** Update File: upd.py",
        "<<<<<<< SEARCH", "    return 1", "=======", "    return 42", ">>>>>>> REPLACE",
        "*** Delete File: del.py",
        "*** Move File: mov.py -> moved.py",
        "*** End Patch")}, Context(d, None))
    check("valid multi-op patch applies (ok)", r.ok)
    check("Add created the file with its content",
          os.path.isfile(os.path.join(d, "new.py")) and "return 2" in _r(os.path.join(d, "new.py")))
    check("Update edited in place", "return 42" in _r(os.path.join(d, "upd.py")))
    check("Delete removed the file", not os.path.exists(os.path.join(d, "del.py")))
    check("Move renamed (old gone, new carries the content)",
          (not os.path.exists(os.path.join(d, "mov.py"))) and "movable" in _r(os.path.join(d, "moved.py")))

    # -- 2. a malformed envelope -> refuse, write nothing ---------------------
    d2 = tempfile.mkdtemp(prefix="applypatch_")
    _w(d2, "keep.py", "orig\n")
    r = patch.apply_patch({"patch": "*** Add File: y.py\n+hi\n"}, Context(d2, None))  # no Begin/End
    check("malformed (no Begin Patch) -> refused, nothing written",
          (not r.ok) and (not os.path.exists(os.path.join(d2, "y.py")))
          and _r(os.path.join(d2, "keep.py")) == "orig\n")

    # -- 3. ATOMICITY: one bad op in the middle -> zero files touched ---------
    d3 = tempfile.mkdtemp(prefix="applypatch_")
    _w(d3, "a.py", "aaa\n")
    _w(d3, "b.py", "bbb\n")
    r = patch.apply_patch({"patch": _env(
        "*** Begin Patch",
        "*** Update File: a.py",
        "<<<<<<< SEARCH", "aaa", "=======", "AAA", ">>>>>>> REPLACE",
        "*** Delete File: b.py",
        "*** Update File: c.py",   # c.py does not exist -> validation fails during planning
        "<<<<<<< SEARCH", "ccc", "=======", "CCC", ">>>>>>> REPLACE",
        "*** End Patch")}, Context(d3, None))
    check("ATOMIC: a later bad op -> refused and NO earlier op applied",
          (not r.ok) and _r(os.path.join(d3, "a.py")) == "aaa\n"
          and os.path.isfile(os.path.join(d3, "b.py")) and _r(os.path.join(d3, "b.py")) == "bbb\n")

    # -- 4. an ambiguous hunk refuses the WHOLE patch -------------------------
    d4 = tempfile.mkdtemp(prefix="applypatch_")
    _w(d4, "amb.py", "x = 1\nx = 1\n")
    r = patch.apply_patch({"patch": _env(
        "*** Begin Patch", "*** Update File: amb.py",
        "<<<<<<< SEARCH", "x = 1", "=======", "x = 9", ">>>>>>> REPLACE",
        "*** End Patch")}, Context(d4, None))
    check("an ambiguous SEARCH hunk -> whole patch refused, file unchanged",
          (not r.ok) and _r(os.path.join(d4, "amb.py")) == "x = 1\nx = 1\n")

    # -- 5. toolset gating ----------------------------------------------------
    config.APPLY_PATCH = False
    check("apply_patch ABSENT from active_tools() when CODE_APPLY_PATCH off",
          "apply_patch" not in {t["name"] for t in active_tools()})
    config.APPLY_PATCH = True
    check("apply_patch PRESENT in active_tools() when CODE_APPLY_PATCH on",
          "apply_patch" in {t["name"] for t in active_tools()})

    passed, total = sum(_results), len(_results)
    print(f"\nVERDICT: {passed}/{total} {'[OK]' if passed == total else '[FAIL]'}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
