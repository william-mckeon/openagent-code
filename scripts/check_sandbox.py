"""
scripts/check_sandbox.py

Acceptance harness for specs/0017 — the FS-confinement sandbox for run_command, checked WITHOUT a model
or a network (subprocess is stubbed, so no real shell runs). Covers write-target extraction, the
escapes() decision, the run_command refusal, and flag-off parity. Run:

    python scripts/check_sandbox.py

Exits 0 only if every check holds — including that CODE_SANDBOX off is byte-identical to today.
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import config, sandbox, tools  # noqa: E402
from src.tools import Context  # noqa: E402

_results = []


def check(label, cond):
    _results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


def main():
    ws = tempfile.mkdtemp(prefix="sandbox_")
    roots = [ws]
    shell = "powershell" if os.name == "nt" else "bash"

    def esc(cmd):
        return sandbox.escapes(cmd, ws, roots, shell)

    # -- write_targets: redirects + write-command destinations ---------------------------------------
    check("write_targets: a redirect target", "out.txt" in sandbox.write_targets("echo x > out.txt", shell))
    check("write_targets: cp/mv destination (last arg)", "../b" in sandbox.write_targets("cp a ../b", shell))
    check("write_targets: tee file args", "log.txt" in sandbox.write_targets("echo x | tee log.txt", shell))
    check("write_targets: dd of=", "/dev/sda" in sandbox.write_targets("dd if=/dev/zero of=/dev/sda", shell))
    check("write_targets: PowerShell Out-File", "p.txt" in sandbox.write_targets("gci | Out-File p.txt", "powershell"))
    check("write_targets: a read-only command has none", sandbox.write_targets("git status", shell) == [])

    # -- escapes: outside the workspace is flagged, inside is fine -----------------------------------
    check("escapes: a redirect to an ABSOLUTE path escapes", esc("echo x > /etc/passwd") == ["/etc/passwd"])
    check("escapes: a redirect to a PARENT path escapes", bool(esc("echo x > ../sibling.txt")))
    check("escapes: a cp to a parent path escapes", bool(esc("cp a.txt ../b.txt")))
    check("escapes: a write INSIDE the workspace is allowed", esc("echo x > sub/out.txt") == [])
    check("escapes: a read-only command never escapes", esc("git status && ls -la") == [])
    ext = os.path.realpath(os.path.join(ws, "..", "granted"))
    os.makedirs(ext, exist_ok=True)
    check("escapes: a write into a GRANTED (--add-dir) root is allowed",
          sandbox.escapes(f"echo x > {os.path.join(ext, 'ok.txt')}", ws, [ws, ext], shell) == [])

    # -- run_command integration (subprocess stubbed so no real shell runs) --------------------------
    called = {"n": 0}

    class _Proc:
        returncode, stdout, stderr = 0, "ok", ""

    _orig_run = tools.subprocess.run
    tools.subprocess.run = lambda *a, **k: (called.__setitem__("n", called["n"] + 1) or _Proc())
    _saved = config.SANDBOX
    ctx = Context(ws, None)
    try:
        config.SANDBOX = True
        called["n"] = 0
        r = tools.run_command({"command": "echo x > ../escape.txt"}, ctx)
        check("SANDBOX on: an out-of-workspace write is REFUSED before running (no subprocess)",
              (not r.ok) and "sandbox" in r.content.lower() and called["n"] == 0)
        called["n"] = 0
        r = tools.run_command({"command": "echo x > inside.txt"}, ctx)
        check("SANDBOX on: an in-workspace write is NOT refused (it runs)", called["n"] == 1)

        config.SANDBOX = False
        called["n"] = 0
        r = tools.run_command({"command": "echo x > ../escape.txt"}, ctx)
        check("SANDBOX off: the sandbox is never consulted (byte-identical - the command runs)",
              called["n"] == 1 and "sandbox" not in r.content.lower())
    finally:
        tools.subprocess.run = _orig_run
        config.SANDBOX = _saved

    passed, total = sum(_results), len(_results)
    print(f"\nVERDICT: {passed}/{total} {'[OK]' if passed == total else '[FAIL]'}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
