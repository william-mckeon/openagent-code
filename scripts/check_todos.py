"""
scripts/check_todos.py

Acceptance harness for specs/0023 — project todos (a persistent, agent-maintained backlog). Dep-free: no
model, no network (pure store + wiring assertions), cloned from scripts/check_memory.py. Proves the store,
the two hardest traps (a hand-edited file still loads; render/parse round-trips), the tool, the wiring, and
the flag-off byte-identical guarantee.

Run:  python scripts/check_todos.py
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import todos, config  # noqa: E402
from src import tools as tools_mod  # noqa: E402
from src.toolset import active_tools  # noqa: E402
from src.prompts import build_system_prompt  # noqa: E402
from src.tools import TOOLS  # noqa: E402
from src.permissions import Permissions, MUTATING  # noqa: E402

_results = []


def check(label, cond):
    _results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


class Ctx:
    def __init__(self, cwd, interactive=False):
        self.cwd = cwd
        self.interactive = interactive


def ws():
    return tempfile.mkdtemp(prefix="todos-check-")


def _statuses(items):
    return [(it["content"], it["status"]) for it in items]


def main():
    _saved = {k: getattr(config, k) for k in ("PROJECT_TODOS", "HOOKS", "GUARDIAN", "PROPOSE")}
    config.HOOKS = config.GUARDIAN = config.PROPOSE = False   # isolate the todos logic
    config.PROJECT_TODOS = True

    # -- the store: load / save / round-trip -------------------------------------------------------------
    a = ws()
    check("load on empty workspace -> []", todos.load(a) == [])

    items = todos.add(todos.load(a), "write the spec")
    todos.save(a, items)
    check("after add+save, load round-trips the item (pending)",
          _statuses(todos.load(a)) == [("write the spec", "pending")])

    items = todos.add(todos.load(a), "add the harness")
    todos.save(a, items)
    check("a second add appends without dropping the first",
          _statuses(todos.load(a)) == [("write the spec", "pending"), ("add the harness", "pending")])

    check("render/parse round-trips (parse(render(items)) == items)",
          todos.parse(todos.render(todos.load(a))) == todos.load(a))

    # -- the hardest trap: a HAND-TYPED file still loads (human-editable) ---------------------------------
    b = ws()
    os.makedirs(os.path.dirname(todos.path(b)), exist_ok=True)
    with open(todos.path(b), "w", encoding="utf-8", newline="") as f:
        f.write("# my notes\n\n- [ ] foo\n- [x] bar\n- [~] baz\n* [X] up\n  - [/] slash\nsome prose here\n- [ ] \n")
    loaded = _statuses(todos.load(b))
    check("a hand-typed checklist loads (bullets/case/whitespace variants), prose + empty items skipped",
          loaded == [("foo", "pending"), ("bar", "done"), ("baz", "in_progress"),
                     ("up", "done"), ("slash", "in_progress")])

    # -- status transforms: the read-modify-write memory can't do ----------------------------------------
    items, err = todos.set_status(todos.load(a), 1, "done")   # by 1-based index
    check("set_status by index flips pending -> done", err is None and items[0]["status"] == "done")
    todos.save(a, items)
    check("the flipped status persists to the file (read-modify-write)",
          todos.load(a)[0]["status"] == "done")
    _, err = todos.set_status(todos.load(a), "add the harness", "in_progress")   # by exact text
    check("set_status by exact text resolves", err is None)
    _, err = todos.set_status(todos.load(a), 99, "done")
    check("set_status on an out-of-range item refuses with a reason", err is not None)

    c2 = todos.add([], "keep me", "pending")
    c2 = todos.add(c2, "drop me", "done")
    check("clear_done drops done items, keeps the rest", _statuses(todos.clear_done(c2)) == [("keep me", "pending")])
    check("outstanding = pending + in_progress only",
          _statuses(todos.outstanding(c2)) == [("keep me", "pending")])
    check("add de-dupes on content (re-add updates status, no duplicate)",
          len(todos.add(todos.add([], "x"), "x", "done")) == 1)

    # -- backlog_text: outstanding-only, whole-line cap, empty -> '' -------------------------------------
    d = ws()
    todos.save(d, [{"content": "outstanding one", "status": "pending"},
                   {"content": "finished", "status": "done"}])
    bt = todos.backlog_text(d)
    check("backlog_text shows outstanding items and EXCLUDES done ones",
          "outstanding one" in bt and "finished" not in bt)
    e = ws()
    todos.save(e, [{"content": "all", "status": "done"}])
    check("backlog_text is '' when nothing is outstanding", todos.backlog_text(e) == "")
    f = ws()
    todos.save(f, [{"content": "X" * 100, "status": "pending"}, {"content": "Y" * 100, "status": "pending"}])
    capped = todos.backlog_text(f, max_chars=60)
    check("backlog_text caps by WHOLE LINES (no mid-line slice) with an elision marker",
          "elided" in capped and all(len(ln) < 200 for ln in capped.splitlines()))

    # per-project isolation
    check("two workspaces don't share todos", todos.load(ws()) == [])

    # -- the tool (end to end via a ctx) -----------------------------------------------------------------
    g = ws()
    ctx = Ctx(g)
    r = tools_mod.project_todos({"action": "add", "content": "ship it"}, ctx)
    check("project_todos add -> ok and the item is on disk",
          r.ok and _statuses(todos.load(g)) == [("ship it", "pending")])
    check("project_todos add with no content refuses",
          tools_mod.project_todos({"action": "add"}, ctx).ok is False)
    tools_mod.project_todos({"action": "done", "item": "1"}, ctx)
    check("project_todos done -> the item is checked off on disk", todos.load(g)[0]["status"] == "done")
    tools_mod.project_todos({"action": "clear"}, ctx)
    check("project_todos clear -> the done item is gone", todos.load(g) == [])
    check("project_todos with an unknown action refuses",
          tools_mod.project_todos({"action": "zap"}, ctx).ok is False)

    # -- wiring: flag gate, prompt injection, non-mutating -----------------------------------------------
    config.PROJECT_TODOS = True
    check("CODE_PROJECT_TODOS on -> project_todos in active_tools()",
          any(t["name"] == "project_todos" for t in active_tools()))
    config.PROJECT_TODOS = False
    check("CODE_PROJECT_TODOS off -> project_todos absent (byte-identical toolset)",
          not any(t["name"] == "project_todos" for t in active_tools()))
    config.PROJECT_TODOS = True

    with_td = build_system_prompt("native", TOOLS, todos="- [ ] TODOTOKEN-123")
    without = build_system_prompt("native", TOOLS, todos=None)
    check("todos text is injected under a 'Project todos' heading",
          "TODOTOKEN-123" in with_td and "Project todos" in with_td)
    check("todos=None leaves the prompt backlog-free", "Project todos" not in without)
    check("an empty/whitespace todos string injects NO block (mirrors memory's guard)",
          "Project todos" not in build_system_prompt("native", TOOLS, todos="   "))

    check("project_todos is NOT in permissions.MUTATING (the agent's tracker, not a project edit)",
          "project_todos" not in MUTATING)
    p = Permissions("plan", {}, [])
    check("project_todos is permitted in PLAN mode (non-mutating, like remember)",
          p.decide("project_todos", {"action": "list"}, Ctx(g)).allowed)

    for k, v in _saved.items():
        setattr(config, k, v)

    passed, total = sum(_results), len(_results)
    print(f"\nVERDICT: {passed}/{total} {'[OK]' if passed == total else '[FAIL]'}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
