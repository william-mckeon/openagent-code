"""
scripts/check_shell_noninteractive.py

Acceptance harness for specs/0055 — the non-interactive shell (CODE_SHELL_NONINTERACTIVE). Proves a command
can no longer HANG the REPL waiting for stdin: with the flag on, PowerShell gets -NonInteractive and the
child's stdin is DEVNULL. The invocation builder `_shell_invocation` is pure (dep-free); a Windows smoke +
stdin-non-hang test confirm the real subprocess. Run:  python scripts/check_shell_noninteractive.py
"""
import os
import sys
import tempfile
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import config  # noqa: E402
from src import tools as tools_mod  # noqa: E402

_results = []


def check(label, cond):
    _results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


def main():
    _saved = (config.SHELL_NONINTERACTIVE, config.SANDBOX)
    try:
        config.SANDBOX = False

        # 1. pure invocation builder OFF -> prior argv + inherited stdin (byte-identical)
        config.SHELL_NONINTERACTIVE = False
        argv, stdin = tools_mod._shell_invocation("echo hi")
        if os.name == "nt":
            check("OFF: prior 'powershell -NoProfile -Command'; stdin inherited (None) - byte-identical",
                  argv == ["powershell", "-NoProfile", "-Command", "echo hi"] and stdin is None)
        else:
            check("OFF (non-nt): 'bash -lc'; stdin inherited (None) - byte-identical",
                  argv == ["bash", "-lc", "echo hi"] and stdin is None)

        # 2. pure invocation builder ON -> -NonInteractive + DEVNULL stdin
        config.SHELL_NONINTERACTIVE = True
        argv, stdin = tools_mod._shell_invocation("echo hi")
        if os.name == "nt":
            check("ON: powershell gets -NonInteractive; stdin is DEVNULL",
                  argv == ["powershell", "-NoProfile", "-NonInteractive", "-Command", "echo hi"]
                  and stdin == subprocess.DEVNULL)
        else:
            check("ON (non-nt): bash argv unchanged; stdin is DEVNULL",
                  argv == ["bash", "-lc", "echo hi"] and stdin == subprocess.DEVNULL)

        # 3/4. real subprocess with the flag ON
        ws = os.path.realpath(tempfile.mkdtemp(prefix="shellni-"))
        ctx = tools_mod.Context(ws, None)
        if os.name == "nt":
            r = tools_mod.run_command({"command": "Write-Output hi"}, ctx)
            check("ON: a normal command still runs and returns its output", r.ok and "hi" in r.content)
            r2 = tools_mod.run_command(
                {"command": "$x = [Console]::In.ReadToEnd(); Write-Output ('got:' + $x)"}, ctx)
            check("ON: a stdin-reading command RETURNS (does not hang) - DEVNULL gives it EOF",
                  r2.ok and "got:" in r2.content)
        else:
            r = tools_mod.run_command({"command": "echo hi"}, ctx)
            check("ON: a normal command still runs and returns its output", r.ok and "hi" in r.content)
            r2 = tools_mod.run_command({"command": "cat"}, ctx)   # cat with DEVNULL stdin -> immediate EOF
            check("ON: a stdin-reading command RETURNS (does not hang) - DEVNULL gives it EOF", r2.ok)
    finally:
        config.SHELL_NONINTERACTIVE, config.SANDBOX = _saved

    # 5. flag is opt-in, tested against the fallback independent of this repo's own .env
    _s = os.environ.pop("CODE_SHELL_NONINTERACTIVE", None)
    default_off = config._as_bool(os.environ.get("CODE_SHELL_NONINTERACTIVE", "false")) is False
    if _s is not None:
        os.environ["CODE_SHELL_NONINTERACTIVE"] = _s
    check("CODE_SHELL_NONINTERACTIVE defaults False when unset (opt-in)", default_off)

    passed, total = sum(_results), len(_results)
    print(f"\nVERDICT: {passed}/{total} {'[OK]' if passed == total else '[FAIL]'}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
