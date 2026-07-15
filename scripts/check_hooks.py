"""
scripts/check_hooks.py

Acceptance harness for specs/0015 — opt-in, FAIL-OPEN lifecycle hooks. Uses REAL subprocess hooks (tiny
python stubs written to a temp dir), no model / no network. Proves the two invariants of every phase plus
the hooks-specific contract:

  * PreToolUse DENY hard-blocks any tool; a PreToolUse 'allow' is no-opinion (tighten-only, can't bypass
    a deny rule).
  * FAIL-OPEN: a crashing / non-JSON / slow (timeout) / no-output hook is ignored (never blocks).
  * PermissionRequest approves/denies an ASK-tier call, headless-only.
  * PostToolUse runs (side effect) but NEVER alters the result.
  * Flag-off (CODE_HOOKS false) is byte-identical to today.

Run:  python scripts/check_hooks.py
"""
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import config, hooks  # noqa: E402
from src.permissions import Permissions  # noqa: E402

_results = []


def check(label, cond):
    _results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


class _Ctx:
    def __init__(self, cwd, depth=0, interactive=False):
        self.cwd = cwd
        self.depth = depth
        self.interactive = interactive


class _Res:
    def __init__(self, ok, content):
        self.ok = ok
        self.content = content


_STUBS = {
    "deny.py":    "import sys,json; sys.stdin.read(); print(json.dumps({'decision':'deny','message':'blocked by test hook'}))",
    "allow.py":   "import sys,json; sys.stdin.read(); print(json.dumps({'decision':'allow','message':'ok by test hook'}))",
    "proceed.py": "import sys; sys.stdin.read()",                       # no output -> no opinion
    "crash.py":   "import sys; sys.stdin.read(); sys.exit(3)",          # non-zero exit, no output
    "nonjson.py": "import sys; sys.stdin.read(); print('not json at all')",
    "sleep.py":   "import sys,time; sys.stdin.read(); time.sleep(2); print('{}')",
    "marker.py":  ("import sys,json,os; json.load(sys.stdin); "
                   "open(os.path.join(os.path.dirname(os.path.abspath(__file__)),'POSTHOOK_RAN'),'w').close()"),
    # path-aware: denies when ANY touched path is under docs/ (reads the runner's uniform `paths` field)
    "deny_paths.py": ("import sys,json\np=json.load(sys.stdin)\n"
                      "if any('docs/' in str(x).lower() for x in (p.get('paths') or [])):\n"
                      "    print(json.dumps({'decision':'deny','message':'a path under docs/'}))"),
}

_PATCH_DOCS = "*** Begin Patch\n*** Update File: docs/FEATURES.md\ngarbage-hunk\n*** End Patch"
_PATCH_README = "*** Begin Patch\n*** Update File: README.md\ngarbage-hunk\n*** End Patch"


def main():
    tmp = tempfile.mkdtemp(prefix="hooks-check-")
    ws = tempfile.mkdtemp(prefix="hooks-ws-")
    for fn, src in _STUBS.items():
        with open(os.path.join(tmp, fn), "w", encoding="utf-8") as f:
            f.write(src)
    hjson = os.path.join(tmp, "hooks.json")
    marker = os.path.join(tmp, "POSTHOOK_RAN")

    def cmd(stub):
        return f'"{sys.executable}" "{os.path.join(tmp, stub)}"'

    def set_hooks(cfg):
        with open(hjson, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
        config.HOOKS_CONFIG = hjson

    ctx = _Ctx(ws)
    _saved_hooks, _saved_cfg = config.HOOKS, config.HOOKS_CONFIG
    config.HOOKS = True

    # -- runner: PreToolUse ------------------------------------------------------------------------------
    set_hooks({"PreToolUse": [{"command": cmd("deny.py")}]})
    v = hooks.pretool("write_file", "x.py", {}, ctx)
    check("pretool: an explicit DENY -> PreVerdict('deny', msg)", v is not None and v.decision == "deny" and "blocked" in v.message)

    set_hooks({"PreToolUse": [{"command": cmd("deny.py"), "tools": ["run_command"]}]})
    check("pretool: a `tools` filter excludes non-listed tools",
          hooks.pretool("write_file", "x.py", {}, ctx) is None and hooks.pretool("run_command", "rm x", {}, ctx) is not None)

    set_hooks({"PreToolUse": [{"command": cmd("proceed.py")}]})
    check("pretool: no output -> no opinion (None)", hooks.pretool("write_file", "x.py", {}, ctx) is None)

    set_hooks({"PreToolUse": [{"command": cmd("crash.py")}]})
    check("pretool: a crashing hook -> FAIL-OPEN (None)", hooks.pretool("write_file", "x.py", {}, ctx) is None)

    set_hooks({"PreToolUse": [{"command": cmd("nonjson.py")}]})
    check("pretool: non-JSON output -> FAIL-OPEN (None)", hooks.pretool("write_file", "x.py", {}, ctx) is None)

    set_hooks({"PreToolUse": [{"command": cmd("sleep.py"), "timeout": 1}]})
    check("pretool: a hook that exceeds its timeout -> FAIL-OPEN (None)", hooks.pretool("write_file", "x.py", {}, ctx) is None)

    set_hooks({"PreToolUse": [{"command": cmd("allow.py")}]})
    check("pretool: an 'allow' verdict is no-opinion (tighten-only) -> None", hooks.pretool("write_file", "x.py", {}, ctx) is None)

    # apply_patch hides its targets INSIDE the patch body — the runner parses them into `paths` so a
    # path-aware hook can gate them (the ride-3 hole where apply_patch slipped the docs/ protection).
    check("payload: apply_patch targets are parsed from the patch body into `paths`",
          hooks._payload("PreToolUse", "apply_patch", "", {"patch": _PATCH_DOCS}, ctx)["paths"] == ["docs/FEATURES.md"])
    set_hooks({"PreToolUse": [{"command": cmd("deny_paths.py")}]})
    check("pretool: a path-aware hook DENIES apply_patch into docs/ (even with a malformed hunk body)",
          hooks.pretool("apply_patch", "", {"patch": _PATCH_DOCS}, ctx) is not None)
    check("pretool: the same hook leaves a non-docs apply_patch alone",
          hooks.pretool("apply_patch", "", {"patch": _PATCH_README}, ctx) is None)
    # a path hook must catch delete_file too (the ride-5 gap where a bulk docs delete slipped)
    check("pretool: a path hook can DENY delete_file into docs/",
          hooks.pretool("delete_file", "docs/x.md", {"path": "docs/x.md"}, ctx) is not None)

    # -- ride-5 Class D: the payload exposes per-turn AGGREGATE so a hook can enforce its own budget -----
    class _AggCtx:
        cwd, depth, interactive = ws, 0, False
        _turn_id, mutations = 4, {"a.py": "edit", "b.py": "write"}
        _destructive_targets = {("delete_file", "x")}
    _pay = hooks._payload("PreToolUse", "delete_file", "docs/x.md", {"path": "docs/x.md"}, _AggCtx())
    check("payload: per-turn aggregate (turn_id / mutations / destructive) is exposed to hooks",
          _pay["turn_id"] == 4 and _pay["mutations"] == 2 and _pay["destructive"] == 1)
    check("payload: delete_file's path rides in `paths` (a path hook can gate deletes)",
          _pay["paths"] == ["docs/x.md"])

    # -- runner: PermissionRequest ----------------------------------------------------------------------
    set_hooks({"PermissionRequest": [{"command": cmd("allow.py")}]})
    pr = hooks.permission_request("edit_file", ".env", ctx)
    check("permission_request: 'allow' -> AskVerdict(approved=True)", pr is not None and pr.approved is True)

    set_hooks({"PermissionRequest": [{"command": cmd("deny.py")}]})
    pr = hooks.permission_request("edit_file", ".env", ctx)
    check("permission_request: 'deny' -> AskVerdict(approved=False)", pr is not None and pr.approved is False)

    set_hooks({"PermissionRequest": [{"command": cmd("proceed.py")}]})
    check("permission_request: no verdict -> None (fall through to guardian/human)",
          hooks.permission_request("edit_file", ".env", ctx) is None)

    # -- runner: PostToolUse (observe-only) --------------------------------------------------------------
    if os.path.exists(marker):
        os.remove(marker)
    set_hooks({"PostToolUse": [{"command": cmd("marker.py")}]})
    res = _Res(True, "done")
    ret = hooks.posttool("write_file", {"path": "x.py"}, res, ctx)
    check("posttool: the hook RUNS (side effect observed)", os.path.exists(marker))
    check("posttool: the result is NEVER altered (observe-only)", ret is None and res.ok is True and res.content == "done")

    # -- integration via decide() ------------------------------------------------------------------------
    set_hooks({"PreToolUse": [{"command": cmd("deny.py")}]})
    p_ae = Permissions("acceptEdits", {}, [])
    d = p_ae.decide("write_file", {"path": "x.py"}, ctx)
    check("decide: a PreToolUse DENY hard-blocks a write that acceptEdits would allow",
          (not d.allowed) and "PreToolUse hook" in d.reason)

    set_hooks({"PreToolUse": [{"command": cmd("allow.py")}]})
    p_deny = Permissions("default", {"deny": ["edit_file(.env)"]}, [])
    d = p_deny.decide("edit_file", {"path": ".env"}, ctx)
    check("decide: a PreToolUse 'allow' can't bypass a deny RULE (hooks only tighten)",
          (not d.allowed) and "deny rule" in d.reason)

    set_hooks({"PermissionRequest": [{"command": cmd("allow.py")}]})
    p_ask = Permissions("default", {"ask": ["edit_file(.env)"]}, [])
    d = p_ask.decide("edit_file", {"path": ".env"}, ctx)   # headless
    check("decide: a PermissionRequest hook approves an ASK-tier call (headless)",
          d.allowed and "PermissionRequest hook approved" in d.reason)

    tgt = p_ask._target("edit_file", {"path": ".env"}, ctx)
    check("headless-only: interactive -> PermissionRequest hook NOT consulted (None)",
          p_ask._hooks_permreq("edit_file", tgt, _Ctx(ws, interactive=True)) is None)

    # -- flag OFF -> byte-identical ----------------------------------------------------------------------
    config.HOOKS = False
    set_hooks({"PreToolUse": [{"command": cmd("deny.py")}]})
    d = Permissions("acceptEdits", {}, []).decide("write_file", {"path": "x.py"}, ctx)
    check("CODE_HOOKS off: the deny hook is never consulted; the write is allowed (unchanged)", d.allowed)

    config.HOOKS, config.HOOKS_CONFIG = _saved_hooks, _saved_cfg

    passed, total = sum(_results), len(_results)
    print(f"\nVERDICT: {passed}/{total} {'[OK]' if passed == total else '[FAIL]'}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
