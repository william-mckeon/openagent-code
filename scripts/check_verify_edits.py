"""
scripts/check_verify_edits.py

Acceptance harness for specs/0014 sub-phase A — the auto-verify module, checked WITHOUT a model or a
network. verify_edits is pure; the subprocess is replaced by a stub run_fn, so no real check runs. Run:

    python scripts/check_verify_edits.py

Exits 0 only if every check holds — including fail-OPEN and the argv-not-shell (no-injection) design.
"""
import os
import sys
import json
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import config, verify_edits  # noqa: E402

_results = []


def check(label, cond):
    _results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


class _Ctx:
    def __init__(self, mutations):
        self.mutations = mutations
        self.cwd = "."


def main():
    # -- verifier_cmds: safe argv default --------------------------------------
    check("verifier_cmds default is the safe py_compile ARGV list (not a shell string)",
          verify_edits.verifier_cmds().get(".py") == ["python", "-m", "py_compile", "{file}"])

    # a valid-JSON NON-dict config ([...] / "x" / 42) has no .items() -> must fail OPEN to the defaults,
    # never raise (the AttributeError that slipped past the OSError/ValueError catch).
    _bad = os.path.join(tempfile.mkdtemp(prefix="vcfg_"), "cmds.json")
    with open(_bad, "w", encoding="utf-8") as f:
        json.dump(["not", "a", "dict"], f)
    _saved = config.VERIFY_CMDS_CONFIG
    config.VERIFY_CMDS_CONFIG = _bad
    try:
        cmds = verify_edits.verifier_cmds()
        raised = False
    except Exception:
        cmds, raised = None, True
    config.VERIFY_CMDS_CONFIG = _saved
    check("a valid-JSON non-dict CODE_VERIFY_CMDS_CONFIG fails open to defaults (never raises)",
          (not raised) and cmds == {".py": ["python", "-m", "py_compile", "{file}"]})

    # -- select: touched write/edit .py only, {file} substituted ---------------
    sel = verify_edits.select({"a.py": "write", "b.py": "edit", "c.md": "write",
                               "d.py": "delete", "e.txt": "write"})
    check("select picks .py writes/edits; skips deletes / non-.py / no-verifier",
          {p for p, _ in sel} == {"a.py", "b.py"})
    check("select substitutes {file} into the argv (a list, no shell string)",
          ["python", "-m", "py_compile", "a.py"] in [argv for _, argv in sel])

    # -- run_checks: FAIL surfaces a problem, PASS is clean --------------------
    fails = verify_edits.run_checks([("a.py", ["x"])], lambda argv: (False, "a.py:3: SyntaxError: bad"))
    check("run_checks surfaces a structured problem on FAIL, naming the file",
          len(fails) == 1 and fails[0]["file"] == "a.py" and "SyntaxError" in fails[0]["error"])
    passes = verify_edits.run_checks([("a.py", ["x"])], lambda argv: (True, ""))
    check("run_checks records a PASS entry (ok=True) — passes are kept for the reward label",
          len(passes) == 1 and passes[0]["ok"] is True and passes[0]["file"] == "a.py")
    check("problems_from surfaces only the FAILING checks as strings",
          verify_edits.problems_from(fails)[0].startswith("a.py") and verify_edits.problems_from(passes) == [])

    # -- fail-OPEN: a raising run_fn yields no problem -------------------------
    def _boom(argv):
        raise RuntimeError("compiler exploded")
    check("fail-OPEN: a run_fn that raises yields NO problem",
          verify_edits.run_checks([("a.py", ["x"])], _boom) == [])

    # -- parse_errors + challenge ---------------------------------------------
    check("parse_errors pulls the file:line:message text",
          "SyntaxError" in verify_edits.parse_errors("a.py:3: SyntaxError: bad\n\n"))
    ch = verify_edits.challenge(["a.py: SyntaxError: bad"])
    check("challenge names the file and stays NON-hijacking (fix only these; no refactor)",
          "a.py" in ch and "do not refactor" in ch.lower())

    # -- a custom CODE_VERIFY_CMDS_CONFIG merges (argv list, .py default kept) --
    cfg = os.path.join(tempfile.mkdtemp(prefix="verifycfg_"), "verify.json")
    with open(cfg, "w", encoding="utf-8") as f:
        json.dump({".ts": ["node", "check.js", "{file}"]}, f)
    config.VERIFY_CMDS_CONFIG = cfg
    cmds = verify_edits.verifier_cmds()
    check("a custom CODE_VERIFY_CMDS_CONFIG merges an argv verifier and keeps the .py default",
          cmds.get(".ts") == ["node", "check.js", "{file}"] and ".py" in cmds)
    config.VERIFY_CMDS_CONFIG = ""

    # -- problems(ctx, stub run_fn): the runtime adapter over ctx.mutations ----
    check("problems() runs the verifier over touched .py files and surfaces a FAIL",
          verify_edits.problems(_Ctx({"a.py": "write"}), run_fn=lambda argv: (False, "a.py:1: bad")) != [])
    check("problems() is clean when every check passes",
          verify_edits.problems(_Ctx({"a.py": "write"}), run_fn=lambda argv: (True, "")) == [])
    check("problems() is clean when nothing was touched (nothing to verify)",
          verify_edits.problems(_Ctx({}), run_fn=lambda argv: (False, "x")) == [])
    check("problems() ignores a deleted path (no verifier for a delete)",
          verify_edits.problems(_Ctx({"a.py": "delete"}), run_fn=lambda argv: (False, "x")) == [])

    # -- hermetic default-off (independent of this repo's .env) ----------------
    _saved = os.environ.pop("CODE_VERIFY_TOUCHED", None)
    default_off = config._as_bool(os.environ.get("CODE_VERIFY_TOUCHED", "false")) is False
    if _saved is not None:
        os.environ["CODE_VERIFY_TOUCHED"] = _saved
    check("CODE_VERIFY_TOUCHED defaults False when unset (opt-in)", default_off)

    passed, total = sum(_results), len(_results)
    print(f"\nVERDICT: {passed}/{total} {'[OK]' if passed == total else '[FAIL]'}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
