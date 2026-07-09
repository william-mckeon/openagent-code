"""
scripts/check_patch_grounding.py

Acceptance harness for specs/0013 sub-phase C — apply_patch's ledger / grounding conformance and the
touched-path manifest, checked WITHOUT a model or a network. Proves the new multi-file tool rides the
EXISTING mutation ledger, so the completion gate (specs/0007) and the RUNTIME grounding gate (specs/0010)
already cover it with no change to either. Run:

    python scripts/check_patch_grounding.py

Exits 0 only if every check holds.
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import patch, grounding  # noqa: E402
from src.agent import _unverified_items  # noqa: E402
from src.tools import Context  # noqa: E402

_results = []


def check(label, cond):
    _results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


def _w(d, name, content):
    p = os.path.join(d, name)
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(content)


def main():
    d = tempfile.mkdtemp(prefix="patchground_")
    _w(d, "src/u.py", "old value\n")
    _w(d, "src/del.py", "gone soon\n")
    _w(d, "src/mv.py", "movable\n")
    ctx = Context(d, None)   # spawn is None -> grounding uses the deterministic runtime path

    r = patch.apply_patch({"patch": "\n".join([
        "*** Begin Patch",
        "*** Add File: src/new.py", "+created",
        "*** Update File: src/u.py",
        "<<<<<<< SEARCH", "old value", "=======", "new value", ">>>>>>> REPLACE",
        "*** Delete File: src/del.py",
        "*** Move File: src/mv.py -> src/mvd.py",
        "*** End Patch"]) + "\n"}, ctx)
    check("apply_patch applied the multi-op patch", r.ok)

    # 1. the mutation ledger records every touched path with the right action (Move = delete + write)
    muts = ctx.mutations
    check("ledger records every touched path (Move = delete old + write new)",
          muts.get("src/new.py") == "write" and muts.get("src/u.py") == "edit"
          and muts.get("src/del.py") == "delete" and muts.get("src/mv.py") == "delete"
          and muts.get("src/mvd.py") == "write")

    # 2. the completion gate clears the plan steps apply_patch backed (add exists / delete gone / new exists)
    ctx.plan_items = [{"content": "add module", "status": "completed", "file": "src/new.py"},
                      {"content": "remove old", "status": "completed", "file": "src/del.py"},
                      {"content": "rename", "status": "completed", "file": "src/mvd.py"}]
    check("agent._unverified_items clears the steps apply_patch backed",
          _unverified_items(ctx) == [])

    # 3. the runtime grounding gate clears an answer that CITES the touched paths (no phantom flag),
    #    including the deleted and old-moved paths that are real changes this run
    answer = ("Added `src/new.py`, updated `src/u.py`, removed `src/del.py`, and moved it to `src/mvd.py`.")
    check("grounding clears an answer citing the touched paths (no phantom-citation flag)",
          grounding.problems(answer, ctx) == [])

    # 4. the ToolResult manifest lists every touched path
    check("the ToolResult manifest lists every touched path",
          all(p in r.content for p in ("src/new.py", "src/u.py", "src/del.py", "src/mv.py", "src/mvd.py")))

    passed, total = sum(_results), len(_results)
    print(f"\nVERDICT: {passed}/{total} {'[OK]' if passed == total else '[FAIL]'}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
