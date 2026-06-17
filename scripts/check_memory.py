"""
scripts/check_memory.py

Acceptance harness for specs/0002-memory.md — cross-session memory, checked WITHOUT
a model or network (pure store + wiring assertions). Run:

    python scripts/check_memory.py

Exits 0 only if every spec checkbox holds. Each line maps to a criterion in the spec.
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import memory, config  # noqa: E402
from src.toolset import active_tools  # noqa: E402
from src.prompts import build_system_prompt  # noqa: E402
from src.tools import TOOLS  # noqa: E402
from src.permissions import Permissions  # noqa: E402


class Ctx:
    def __init__(self, cwd, interactive=False):
        self.cwd = cwd
        self.interactive = interactive


_results = []


def check(label, cond):
    _results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


def ws():
    return tempfile.mkdtemp(prefix="mem-check-")


def main():
    # load on an empty workspace -> ""
    a = ws()
    check("load on empty workspace -> ''", memory.load(a) == "")

    # remember persists and reloads
    memory.remember(a, "note A")
    check("after remember, load contains the note", "note A" in memory.load(a))

    # second remember appends
    memory.remember(a, "note B")
    both = memory.load(a)
    check("second remember appends (both present)", "note A" in both and "note B" in both)

    # cap keeps the most-recent tail
    b = ws()
    memory.remember(b, "OLDEST_" + "x" * 200)
    memory.remember(b, "NEWEST_MARKER")
    capped = memory.load(b, max_chars=30)
    check("load caps to the recent tail", "NEWEST_MARKER" in capped and "OLDEST" not in capped and "elided" in capped)

    # per-project isolation
    c = ws()
    check("two workspaces don't share memory", memory.load(c) == "")

    # the remember tool is gated by CODE_MEMORY
    saved = config.MEMORY
    try:
        config.MEMORY = True
        check("CODE_MEMORY on -> remember in active_tools()",
              any(t["name"] == "remember" for t in active_tools()))
        config.MEMORY = False
        check("CODE_MEMORY off -> remember absent",
              not any(t["name"] == "remember" for t in active_tools()))
    finally:
        config.MEMORY = saved

    # memory injected into the system prompt, and absent when None
    with_mem = build_system_prompt("native", TOOLS, memory="MEMTOKEN-123")
    without = build_system_prompt("native", TOOLS, memory=None)
    check("memory text appears under a memory heading",
          "MEMTOKEN-123" in with_mem and "Project memory" in with_mem)
    check("memory=None leaves the prompt memory-free", "Project memory" not in without)

    # remember is non-mutating -> allowed in plan mode (still its own decision)
    p = Permissions("plan", {}, [])
    check("remember is permitted in plan mode (non-mutating)",
          p.decide("remember", {"note": "x"}, Ctx(a)).allowed)

    passed, total = sum(_results), len(_results)
    print(f"\nVERDICT: {passed}/{total} {'[OK]' if passed == total else '[FAIL]'}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
