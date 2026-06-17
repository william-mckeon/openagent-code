"""
scripts/check_permissions.py

Acceptance harness for specs/0001-permissions.md — the permission engine, checked
WITHOUT a model or a network (pure decide() assertions). Run:

    python scripts/check_permissions.py

Exits 0 only if every spec checkbox holds. Each line maps to a criterion in the spec.
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src.permissions import Permissions  # noqa: E402


class Ctx:
    """Minimal stand-in for the tool Context — decide() only needs cwd + interactive."""
    def __init__(self, cwd, interactive=False):
        self.cwd = cwd
        self.interactive = interactive


_results = []


def check(label, cond):
    _results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


def allowed(perms, ctx, tool, **args):
    return perms.decide(tool, args, ctx).allowed


def main():
    ws = tempfile.mkdtemp(prefix="perm-check-")
    ctx = Ctx(ws)
    inside = "foo.py"
    outside_abs = os.path.realpath(os.path.join(ws, "..", "outside.txt"))

    # plan mode — read-only
    p = Permissions("plan", {}, [])
    check("plan blocks write_file", not allowed(p, ctx, "write_file", path=inside, content="x"))
    check("plan blocks edit_file", not allowed(p, ctx, "edit_file", path=inside, old_string="a", new_string="b"))
    check("plan blocks run_command", not allowed(p, ctx, "run_command", command="echo hi"))
    check("plan allows read_file", allowed(p, ctx, "read_file", path=inside))
    check("plan allows grep", allowed(p, ctx, "grep", pattern="x", path="."))

    # acceptEdits — auto-edit, gate commands
    p = Permissions("acceptEdits", {}, [])
    check("acceptEdits allows edit_file", allowed(p, ctx, "edit_file", path=inside, old_string="a", new_string="b"))
    check("acceptEdits still gates run_command (headless -> block)",
          not allowed(p, ctx, "run_command", command="pytest"))

    # deny beats everything, including bypass
    p = Permissions("bypass", {"deny": ["run_command(rm:*)"]}, [])
    check("deny rm wins under bypass", not allowed(p, ctx, "run_command", command="rm -rf x"))
    check("bypass allows a non-denied command", allowed(p, ctx, "run_command", command="echo hi"))

    # deny beats allow
    p = Permissions("bypass", {"deny": ["run_command(rm:*)"], "allow": ["run_command(rm:*)"]}, [])
    check("deny beats allow", not allowed(p, ctx, "run_command", command="rm file"))

    # ask, no human present -> blocked (never silently allowed)
    p = Permissions("default", {"ask": ["run_command(git push:*)"]}, [])
    check("ask + no human -> blocked", not allowed(p, ctx, "run_command", command="git push origin main"))

    # workspace fence
    p = Permissions("bypass", {}, [])
    check("fence blocks absolute path outside workspace", not allowed(p, ctx, "read_file", path=outside_abs))
    check("fence blocks '..' escape", not allowed(p, ctx, "write_file", path="../escape.txt", content="x"))
    check("inside-workspace path allowed", allowed(p, ctx, "read_file", path=inside))
    p2 = Permissions("bypass", {}, [os.path.realpath(os.path.dirname(outside_abs))])
    check("CODE_ADD_DIRS widens the fence", allowed(p2, ctx, "read_file", path=outside_abs))

    # mode default + rules, command-prefix matcher
    p = Permissions("default", {"allow": ["run_command(pytest:*)"]}, [])
    check("default headless blocks an unlisted command", not allowed(p, ctx, "run_command", command="echo hi"))
    check("allow rule permits 'pytest -q'", allowed(p, ctx, "run_command", command="pytest -q"))

    # path-glob matcher
    p = Permissions("default", {"allow": ["edit_file(src/**)"]}, [])
    check("allow edit_file(src/**) matches src/app.py",
          allowed(p, ctx, "edit_file", path="src/app.py", old_string="a", new_string="b"))
    check("allow edit_file(src/**) does NOT match other.py",
          not allowed(p, ctx, "edit_file", path="other.py", old_string="a", new_string="b"))

    passed, total = sum(_results), len(_results)
    print(f"\nVERDICT: {passed}/{total} {'[OK]' if passed == total else '[FAIL]'}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
