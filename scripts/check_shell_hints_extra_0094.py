"""
scripts/check_shell_hints_extra_0094.py

Acceptance harness for specs/0094 — the extra shell hints + scratch-file discipline line. Dep-free. Proves: with
CODE_SHELL_HINTS_EXTRA on (and shell_hints on, Windows) the env block gains ONE line naming the heredoc trap, the
`ls -la` / `which` / `head`/`tail` mappings the lean block dropped, and the `$env:TEMP` scratch discipline; with it
off the env block is BYTE-IDENTICAL (no extra line), independent of lean/full.

    python scripts/check_shell_hints_extra_0094.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import envcontext   # noqa: E402  (envcontext is stdlib-only; no litellm stub needed)

_results = []


def check(label, cond):
    _results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


_MARKER = "more shell rules"


def main():
    if os.name != "nt":
        # the extra block is Windows-only (PowerShell); assert the OFF path is byte-identical everywhere and skip
        off = envcontext.build_env_context("C:/x", shell_hints=True, extra_hints=False)
        on = envcontext.build_env_context("C:/x", shell_hints=True, extra_hints=True)
        check("non-Windows: extra_hints is a no-op (env block identical on/off)", off == on and _MARKER not in on)
        print("  (skipping the Windows-only extra-hints content checks)")
        passed, total = sum(_results), len(_results)
        print(f"\nVERDICT: {passed}/{total} {'[OK]' if passed == total else '[FAIL]'}")
        return 0 if passed == total else 1

    # -- OFF: byte-identical, independent of lean/full ---------------------------------------------------
    for lean in (False, True):
        off = envcontext.build_env_context("C:/x", shell_hints=True, lean=lean, extra_hints=False)
        check(f"OFF (lean={lean}): no extra line — byte-identical", _MARKER not in off)

    # -- extra_hints requires shell_hints (no shell block -> no extra line) ------------------------------
    check("extra_hints without shell_hints adds nothing",
          _MARKER not in envcontext.build_env_context("C:/x", shell_hints=False, extra_hints=True))

    # -- ON: the extra line is present and covers each gap ----------------------------------------------
    for lean in (False, True):
        on = envcontext.build_env_context("C:/x", shell_hints=True, lean=lean, extra_hints=True)
        off = envcontext.build_env_context("C:/x", shell_hints=True, lean=lean, extra_hints=False)
        check(f"ON (lean={lean}): exactly one extra line is appended to the OFF block",
              on.startswith(off) and _MARKER in on and on.count(_MARKER) == 1)
        check(f"ON (lean={lean}): names the heredoc trap (cat << 'EOF' / no heredoc)",
              "heredoc" in on and "<<" in on and "Set-Content" in on)
        check(f"ON (lean={lean}): restores the dropped Unix-flag traps (ls -la / which / head / tail)",
              "ls -la" in on and "Get-ChildItem" in on and "Get-Command" in on
              and "head" in on and "tail" in on)
        check(f"ON (lean={lean}): carries the scratch-file discipline ($env:TEMP, not the workspace)",
              "$env:TEMP" in on and "scratch" in on.lower() and "workspace" in on)

    passed, total = sum(_results), len(_results)
    print(f"\nVERDICT: {passed}/{total} {'[OK]' if passed == total else '[FAIL]'}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
