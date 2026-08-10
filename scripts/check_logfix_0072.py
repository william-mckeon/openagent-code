"""
scripts/check_logfix_0072.py

Acceptance harness for specs/0072 — four bugs the logs/*.log review surfaced. Dep-free (no model, no network):
exercises Registry arg-validation, the depth-aware propose deny, the PowerShell UTF-8 prelude, and the
2>&1 shell hint directly. Run:

    python scripts/check_logfix_0072.py
"""
import os
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import permissions as P, envcontext            # noqa: E402
from src.tools import Registry, ToolResult, _PS_UTF8_PRELUDE, _shell_invocation  # noqa: E402

_results = []


def check(label, cond):
    _results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


def main():
    # N1: a missing required arg returns a CLEAN message, not a raw KeyError leaked as the tool result
    reg = Registry([{"name": "t", "fn": lambda a, c: ToolResult(True, "ok"),
                     "parameters": {"type": "object", "properties": {}, "required": ["path"]}}])
    r_missing = reg.run("t", {}, None)
    check("N1: a missing required arg -> 'missing required argument: path' (no raw KeyError)",
          not r_missing.ok and "missing required argument" in r_missing.content
          and "path" in r_missing.content and "KeyError" not in r_missing.content)
    check("N1: a valid call still dispatches; a NON-dict args value is treated as no args (no crash)",
          reg.run("t", {"path": "x"}, None).ok is True
          and "missing required argument" in reg.run("t", "notadict", None).content)
    check("N1: a tool with no required args is unaffected (byte-identical dispatch)",
          Registry([{"name": "z", "fn": lambda a, c: ToolResult(True, "z"),
                     "parameters": {"required": []}}]).run("z", {}, None).ok is True)

    # N2: the propose read-only deny is DEPTH-AWARE — a subagent gets a terminal, honest message
    pm = P.Permissions("propose", {}, [])
    top = pm._propose_ro_msg(types.SimpleNamespace(depth=0))
    child = pm._propose_ro_msg(types.SimpleNamespace(depth=1))
    check("N2: top-level (depth 0) keeps the original 'read-only until the manifest is approved' text",
          top == "propose mode is read-only until the manifest is approved")
    check("N2: a SUBAGENT (depth>0) is told it cannot approve and to report up — do NOT retry",
          "SUBAGENT cannot approve" in child and "do NOT retry" in child and "top-level agent" in child)

    # N3: the PowerShell invocation forces UTF-8 output so it matches run_command's utf-8 decode
    check("N3: the UTF-8 prelude sets Console.OutputEncoding to a no-BOM UTF8Encoding",
          "OutputEncoding" in _PS_UTF8_PRELUDE and "UTF8Encoding" in _PS_UTF8_PRELUDE)
    argv, _stdin = _shell_invocation("Get-Content x.txt")
    if os.name == "nt":
        check("N3 (nt): the prelude is prepended and the real command follows unchanged",
              _PS_UTF8_PRELUDE in argv[-1] and argv[-1].endswith("Get-Content x.txt")
              and argv[0] == "powershell")
    else:
        check("N3 (posix): bash invocation is unchanged (prelude is nt-only, byte-identical)",
              argv[0] == "bash" and _PS_UTF8_PRELUDE not in " ".join(argv))

    # N4: the shell hint warns against 2>&1 on a native exe (rides CODE_SHELL_HINTS, nt-only)
    ws = tempfile.mkdtemp(prefix="lf72_")
    block = envcontext.build_env_context(ws, None, shell_hints=True)
    if os.name == "nt":
        check("N4 (nt): the shell hints warn against 2>&1 on a native exe (NativeCommandError / mislabeled)",
              "2>&1" in block and "NativeCommandError" in block)
    else:
        check("N4 (posix): the nt-only shell hint does not render (byte-identical)",
              "NativeCommandError" not in block)
    check("N4: with shell_hints OFF the hint never renders (byte-identical)",
          "NativeCommandError" not in envcontext.build_env_context(ws, None, shell_hints=False))

    passed, total = sum(_results), len(_results)
    print(f"\nVERDICT: {passed}/{total} {'[OK]' if passed == total else '[FAIL]'}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
