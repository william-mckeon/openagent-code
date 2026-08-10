"""
scripts/check_security_0071.py

Acceptance harness for specs/0071 — four security-boundary fixes from the full bug hunt. Dep-free (no model,
no network): exercises the permission engine, the tool-alias canonicalization, and the goal entry filter
directly. Run:

    python scripts/check_security_0071.py
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import permissions as P, goal   # noqa: E402
from src.tools import Context            # noqa: E402

_results = []


def check(label, cond):
    _results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


def main():
    # 1. self-kill guard (permissions.py) — the PowerShell pipe bypass
    check("self-kill: the pipe form 'Get-Process python | Stop-Process' IS flagged (the live bypass)",
          P._is_self_kill("Get-Process python | Stop-Process")
          and P._is_self_kill("Get-Process -Name python | Stop-Process")
          and P._is_self_kill("gps python | kill"))
    check("self-kill: the direct forms still flag (Stop-Process -Name python / taskkill /IM python[3].exe / pkill)",
          P._is_self_kill("Stop-Process -Name python") and P._is_self_kill("taskkill /IM python.exe")
          and P._is_self_kill("taskkill /IM python3.exe") and P._is_self_kill("pkill python"))
    check("self-kill: killing ANOTHER process beside an unrelated python call is NOT flagged (two statements)",
          not P._is_self_kill("Stop-Process -Name foo; python run.py")
          and not P._is_self_kill("Stop-Process -Name notepad && python x"))
    check("self-kill: kill-by-PID is NOT flagged (no python token)",
          not P._is_self_kill("Stop-Process -Id 1234") and not P._is_self_kill("kill 4567"))

    # 2. print_tree fence bypass (alias resolved BEFORE the gate now)
    check("alias: _canonical_tool maps print_tree -> tree, leaves other names alone",
          P._canonical_tool("print_tree") == "tree" and P._canonical_tool("read_file") == "read_file")
    ws = tempfile.mkdtemp(prefix="sec71_")
    perms = P.Permissions("bypass", {}, [])
    ctx = Context(ws, perms)
    outside = os.path.abspath(os.sep)   # filesystem root, outside the workspace fence
    check("fence: BOTH tree and its alias print_tree are DENIED outside the workspace (no fence escape)",
          not perms.decide("tree", {"path": outside}, ctx).allowed
          and not perms.decide("print_tree", {"path": outside}, ctx).allowed)
    check("fence: print_tree INSIDE the workspace is still allowed (the fix doesn't over-block)",
          perms.decide("print_tree", {"path": ws}, ctx).allowed)

    # 3. /add-dir read-only enforcement — cli.py now routes the grant to read_only_roots
    ref = os.path.realpath(tempfile.mkdtemp(prefix="ref71_"))
    perms2 = P.Permissions("acceptEdits", {}, [])
    perms2.read_only_roots.append(ref)   # exactly what _repl_add_dir does after specs/0071
    c2 = Context(ws, perms2)
    check("/add-dir: a 'granted (read)' ref dir DENIES writes in acceptEdits and ALLOWS reads (msg == enforcement)",
          not perms2.decide("write_file", {"path": os.path.join(ref, "x.py")}, c2).allowed
          and perms2.decide("read_file", {"path": os.path.join(ref, "x.py")}, c2).allowed)

    # 4. goal entry filter (goal.py) — versioned / alternate interpreter with inline code
    check("entry_ok: versioned/alt interpreters with an inline-code flag are refused (python3.12/pythonw/nodejs)",
          goal.entry_ok(["python3.12", "-c", "import os"])[0] is False
          and goal.entry_ok(["pythonw", "-c", "x"])[0] is False
          and goal.entry_ok(["nodejs", "-e", "x"])[0] is False
          and goal.entry_ok(["python.exe", "-c", "x"])[0] is False)
    check("entry_ok: a legit non-inline bar still passes (pytest / python -m pytest)",
          goal.entry_ok(["pytest", "-q"])[0] is True and goal.entry_ok(["python", "-m", "pytest"])[0] is True)

    passed, total = sum(_results), len(_results)
    print(f"\nVERDICT: {passed}/{total} {'[OK]' if passed == total else '[FAIL]'}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
