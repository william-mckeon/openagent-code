"""
scripts/check_verify_gate.py

Acceptance harness for specs/0014 sub-phase B — the auto-verify gate wired into the agent loop, checked
WITHOUT a model or a network. A scripted planner emits a no-tool-call "done" each step; verify_edits.results
is monkeypatched to canned outcomes (no real subprocess), so the gate's control flow is exercised
deterministically. Run:

    python scripts/check_verify_gate.py

Exits 0 only if every check holds — including that the flag OFF path is byte-identical to today.
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import config, verify_edits  # noqa: E402
from src.agent import Agent  # noqa: E402
from src.context import ContextManager  # noqa: E402
from src.tools import Context  # noqa: E402

_results = []


def check(label, cond):
    _results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


class _Decision:
    def __init__(self, final):
        self.assistant = {"role": "assistant", "content": final}
        self.final = final
        self.calls = []          # no tool calls -> the completion/verify/grounding branch
        self.nudge = None
        self.gave_up = False


class _Planner:
    def step(self, context, step):
        return _Decision("Done — the change is in place.")

    def format_result(self, call, result):
        return {"role": "tool", "content": ""}


class _Model:
    def summarize(self, msgs):
        return "summary"


class _Traj:
    def __init__(self):
        self.steps = 0
        self.tool_calls = 0
        self.verifs = []            # (cmd, ok, output) rewards logged by the gate

    def log_turn(self, m): pass
    def log_compaction(self, *a): pass
    def log_tool_call(self, *a, **k): pass
    def log_permission(self, *a, **k): pass
    def log_verification(self, cmd, ok, output): self.verifs.append((cmd, ok, output))


def _agent(traj):
    cm = ContextManager("system", _Model(), traj, compact_at_tokens=0)
    return Agent(_Planner(), None, traj, 6, cm)


def _ctx():
    c = Context(tempfile.mkdtemp(prefix="verifygate_"), None)
    c.mutations = {"a.py": "write"}   # a touched .py so the gate has something to verify
    return c


_FAIL = [{"file": "a.py", "cmd": "python -m py_compile a.py", "ok": False,
          "error": "SyntaxError", "output": "boom"}]
_PASS = [{"file": "a.py", "cmd": "python -m py_compile a.py", "ok": True, "error": "", "output": ""}]


def main():
    _orig = verify_edits.results
    config.VERIFY_TOUCHED = True
    config.VERIFY_TOUCHED_RETRIES = 2
    config.VERIFY_TOUCHED_LABEL = True

    # 1. persistent failure -> bounded retries -> honest 'verify_failed_edits' (+ rewards logged)
    verify_edits.results = lambda ctx, run_fn=None: _FAIL
    traj = _Traj()
    r = _agent(traj).run("fix a.py", _ctx())
    check("persistent verify failure -> bounded retries then honest 'verify_failed_edits'",
          r.terminated == "verify_failed_edits")
    check("each failing check is logged as a reward (CODE_VERIFY_TOUCHED_LABEL)",
          ("python -m py_compile a.py", False, "boom") in traj.verifs)

    # 2. fail once, then pass (the reflection loop lands the fix) -> the run completes
    n = {"i": 0}

    def _flip(ctx, run_fn=None):
        n["i"] += 1
        return _FAIL if n["i"] == 1 else _PASS
    verify_edits.results = _flip
    traj = _Traj()
    r = _agent(traj).run("fix a.py", _ctx())
    check("verify fails once then passes -> the run does NOT end verify_failed_edits",
          r.terminated != "verify_failed_edits")
    check("only the FINAL passing check is logged — the intermediate FAILURE is NOT (specs/0014 corpus rule)",
          any(ok is True for _, ok, _ in traj.verifs)
          and all(ok is True for _, ok, _ in traj.verifs))   # no failing record survived the reflection loop

    # 3. flag OFF -> the gate is skipped entirely (no verify, no reward), byte-identical to today
    config.VERIFY_TOUCHED = False
    verify_edits.results = lambda ctx, run_fn=None: _FAIL   # would fail IF it were consulted
    traj = _Traj()
    r = _agent(traj).run("do a thing", _ctx())
    check("CODE_VERIFY_TOUCHED off -> gate skipped (no verify_failed_edits, no reward records)",
          r.terminated != "verify_failed_edits" and traj.verifs == [])

    # 4. label OFF -> the gate still RUNS (sets ctx._verified_ok) but records no reward. Asserting BOTH proves
    #    the gate ran, not that it was skipped — with canned _PASS results an empty verifs list alone is
    #    observationally identical to a skipped gate (specs/0077: the check was vacuous without _verified_ok).
    config.VERIFY_TOUCHED = True
    config.VERIFY_TOUCHED_LABEL = False
    verify_edits.results = lambda ctx, run_fn=None: _PASS
    traj = _Traj()
    ctx4 = _ctx()
    _agent(traj).run("do a thing", ctx4)
    check("CODE_VERIFY_TOUCHED_LABEL off -> the gate RAN (ctx._verified_ok set) but logged NO reward",
          traj.verifs == [] and getattr(ctx4, "_verified_ok", False) is True)

    # 5. cross-turn hijack fix: a stale completed-but-unbacked plan step left over from a PRIOR task is
    #    reset at the start of the next run, so the completion gate can't hijack the new, unrelated turn
    #    (seen live: "what project is this?" answered with a stale favicon status + "I exhausted my budget").
    _vc, _vg = config.VERIFY_COMPLETION, config.VERIFY_GROUNDING
    config.VERIFY_TOUCHED = False
    config.VERIFY_COMPLETION = True
    config.VERIFY_GROUNDING = False
    traj = _Traj()
    ctx = _ctx()
    ctx.plan_items = [{"content": "make favicons", "status": "completed", "file": "ghost.png"}]  # never mutated
    ctx.spawn_count = 8   # a prior task's fan-out budget left on the reused REPL ctx
    r = _agent(traj).run("what project is this?", ctx)
    check("a stale completed plan step from a prior task does NOT hijack a new turn (per-task reset)",
          r.terminated != "unverified_completion" and ctx.plan_items == [])
    check("the subagent fan-out counter is reset per task (no cross-turn spawn_agent block)",
          ctx.spawn_count == 0)
    config.VERIFY_COMPLETION, config.VERIFY_GROUNDING = _vc, _vg

    # 6. path-normalization: a step whose file was edited via an ABSOLUTE path is recognized as backed
    #    (its file normalizes to the SAME _rel(_abs(...)) key the mutation ledger uses), so a real change
    #    no longer reads as "not backed" - the false completion challenge seen editing centpilot via abs paths.
    from src.agent import _unverified_items
    from src.tools import _record_mutation
    c = _ctx()
    c.mutations = {}
    abspath = os.path.join(c.cwd, "sub", "x.py")
    os.makedirs(os.path.dirname(abspath), exist_ok=True)
    open(abspath, "w").close()
    _record_mutation(c, abspath, "write")          # the edit went through an ABSOLUTE path
    c.plan_items = [{"content": "edit x", "status": "completed", "file": abspath}]  # step names the ABS path
    check("a step completed via an absolute path reads as backed (no false 'not backed')",
          _unverified_items(c) == [])

    # 7. case-fold: the ledger match is case-insensitive on Windows (matching the case-insensitive FS +
    #    os.path.exists) and case-SENSITIVE on POSIX (correct on the Linux training substrate). A plan
    #    step naming a file with different casing than the edit call still matches its real change.
    c = _ctx()
    c.mutations = {}
    os.makedirs(os.path.join(c.cwd, "src"), exist_ok=True)
    open(os.path.join(c.cwd, "src", "App.py"), "w").close()
    _record_mutation(c, "src/App.py", "edit")                       # ledger key keeps original casing
    c.plan_items = [{"content": "x", "status": "completed", "file": "src/app.py"}]  # step: different case
    expect_backed = os.path.normcase("src/App.py") == os.path.normcase("src/app.py")
    check("completion-gate ledger match is case-insensitive on Windows / case-sensitive on POSIX",
          (_unverified_items(c) == []) == expect_backed)

    # 8. read-only integrity (specs/0065): a review / placeholder step must NOT trap the gate, while a real
    #    create/edit that didn't land STILL flags. The live failure: a read-only review carried plan steps
    #    named 'N/A' / 'Centpilot' (never in the ledger), so the gate escalated the agent into writing junk
    #    files to "back up" unsatisfiable steps.
    from src.agent import _is_checkable_target, _completion_challenge, _unapplied_manifest
    c = _ctx()
    c.mutations = {}
    os.makedirs(os.path.join(c.cwd, "subdir"), exist_ok=True)
    open(os.path.join(c.cwd, "real.py"), "w").close()
    check("_is_checkable_target: a directory target is NOT checkable ('Centpilot' / 'subdir')",
          _is_checkable_target(c, "subdir") is False)
    check("_is_checkable_target: a bare placeholder ('N/A' / 'TBD') is NOT checkable",
          _is_checkable_target(c, "N/A") is False and _is_checkable_target(c, "TBD") is False)
    check("_is_checkable_target: an existing file IS checkable (an edit that should have landed)",
          _is_checkable_target(c, "real.py") is True)
    check("_is_checkable_target: a foo.py create target (extension, not yet on disk) IS checkable",
          _is_checkable_target(c, "brand_new.py") is True)
    check("_is_checkable_target: a well-known EXTENSIONLESS create (Dockerfile / docker/Makefile) IS checkable",
          _is_checkable_target(c, "Dockerfile") is True and _is_checkable_target(c, "docker/Makefile") is True)
    check("_is_checkable_target: an extensionless free-text label is NOT checkable (can't match the allowlist)",
          _is_checkable_target(c, "review conversation") is False)

    c.plan_items = [{"content": "review conversation", "status": "completed", "file": "N/A"},
                    {"content": "review folder", "status": "completed", "file": "Centpilot"},
                    {"content": "review subdir", "status": "completed", "file": "subdir"}]
    check("read-only review steps ('N/A' / 'Centpilot' / a subdir) do NOT trap the gate (returns [])",
          _unverified_items(c) == [])

    c.plan_items = [{"content": "create the module", "status": "completed", "file": "brand_new.py"}]
    check("a create target that never appeared in the ledger STILL flags (the create didn't happen)",
          _unverified_items(c) != [])
    c.plan_items = [{"content": "edit the module", "status": "completed", "file": "real.py"}]
    check("an existing-but-unmutated file step STILL flags (an edit that didn't land)",
          _unverified_items(c) != [])
    c.plan_items = [{"content": "add the Dockerfile", "status": "completed", "file": "Dockerfile"}]
    check("a never-written Dockerfile create STILL flags (extensionless allowlist keeps the honest catch)",
          _unverified_items(c) != [])

    txt = _completion_challenge(["'N/A' - marked done but nothing changed it this session"])
    check("the completion challenge offers the read-only exit (drop the file with update_plan; never fabricate)",
          "update_plan" in txt and "NEVER create, edit, or delete a file" in txt)

    c.manifest = {"approved": True, "items": [{"action": "add", "path": "subdir"}]}
    c.mutations = {}
    check("_unapplied_manifest: an approved DIRECTORY target is not reported unapplied (specs/0065)",
          _unapplied_manifest(c) == [])
    c.manifest = {"approved": True, "items": [{"action": "add", "path": "still_missing.py"}]}
    check("_unapplied_manifest: an approved FILE target that never landed IS still reported unapplied",
          _unapplied_manifest(c) != [])

    verify_edits.results = _orig
    config.VERIFY_TOUCHED = False
    config.VERIFY_TOUCHED_LABEL = True

    passed, total = sum(_results), len(_results)
    print(f"\nVERDICT: {passed}/{total} {'[OK]' if passed == total else '[FAIL]'}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
