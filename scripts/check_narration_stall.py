"""
scripts/check_narration_stall.py

Acceptance harness for specs/0067 — the narration-stall guard, checked WITHOUT a model or network. A scripted
planner emits a pure `Write-Output "..."` run_command every step (a fake registry executes it as a no-op ok),
so the guard's control flow is exercised deterministically: the streak trips, a bounded nudge fires, then the
run ends honestly as 'narration_stall'. Also proves the detector's conservatism, the outcome registration, and
that the flag OFF path is byte-identical (the run just goes to max_steps as before). Run:

    python scripts/check_narration_stall.py
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import config, outcomes                # noqa: E402
from src.agent import Agent, _is_narration_command  # noqa: E402
from src.context import ContextManager          # noqa: E402
from src.tools import Context, ToolResult       # noqa: E402
from src.permissions import Permissions         # noqa: E402

_results = []


def check(label, cond):
    _results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


class _Decision:
    def __init__(self, cmd):
        self.assistant = {"role": "assistant", "content": ""}
        self.final = ""
        self.calls = [{"name": "run_command", "args": {"command": cmd}}]
        self.nudge = None
        self.gave_up = False
        self.dropped = False


class _NarrPlanner:
    """Emits the same pure-narration run_command every step (ignores the nudge, like a stuck weak model)."""
    def __init__(self, cmd='Write-Output "Status: waiting for the user to pick A/B/C."'):
        self.cmd = cmd

    def step(self, context, step):
        return _Decision(self.cmd)

    def format_result(self, call, result):
        return {"role": "tool", "content": ""}


class _Reg:
    def run(self, name, args, ctx):
        return ToolResult(True, "")          # the narration print "succeeds" but changes nothing


class _Model:
    def summarize(self, msgs):
        return "summary"


class _Traj:
    def __init__(self):
        self.steps = 0
        self.tool_calls = 0

    def log_turn(self, m): pass
    def log_compaction(self, *a): pass
    def log_tool_call(self, *a, **k): pass
    def log_permission(self, *a, **k): pass
    def log_verification(self, *a, **k): pass


def _agent(traj, planner, reg, max_steps=12):
    cm = ContextManager("system", _Model(), traj, compact_at_tokens=0)
    return Agent(planner, reg, traj, max_steps, cm)


def _ctx():
    c = Context(tempfile.mkdtemp(prefix="narr_"), Permissions("bypass", {}, []))
    c.verbose = False
    return c


def main():
    _saved = {k: getattr(config, k) for k in (
        "GUARD_NARRATION_STALL", "NARRATION_STALL_MAX", "NARRATION_STALL_RETRIES", "NARRATION_AS_FINAL",
        "ADAPTIVE_EFFORT", "SITUATIONAL_CONTEXT",
        "VERIFY_COMPLETION", "VERIFY_MANIFEST", "VERIFY_GROUNDING", "VERIFY_TOUCHED")}
    # specs/0085 supersedes the narration guard for the reply case; isolate it OFF so this harness tests the
    # 0067 guard's own control flow (the live .env may arm CODE_NARRATION_AS_FINAL).
    config.NARRATION_AS_FINAL = False
    config.ADAPTIVE_EFFORT = config.SITUATIONAL_CONTEXT = False
    config.VERIFY_COMPLETION = config.VERIFY_MANIFEST = config.VERIFY_GROUNDING = config.VERIFY_TOUCHED = False
    config.NARRATION_STALL_MAX = 3
    config.NARRATION_STALL_RETRIES = 1

    # 1. the detector — conservative: only an obvious pure print trips it
    check("_is_narration_command: Write-Output / echo / Write-Host of a literal are narration",
          _is_narration_command('Write-Output "Status: x"')
          and _is_narration_command("echo 'done for now'")
          and _is_narration_command('Write-Host "waiting"'))
    check("_is_narration_command: a read / a pipe / a redirect / a subshell is NOT narration",
          not _is_narration_command("Get-Content foo.txt")
          and not _is_narration_command("ls -Recurse")
          and not _is_narration_command('echo x | Set-Content f.txt')
          and not _is_narration_command('Write-Output "x" > f.txt')
          and not _is_narration_command('Write-Output (Get-Content f)')
          and not _is_narration_command('Write-Output "a"; Remove-Item b'))
    # specs/0069: operators INSIDE the quoted message are prose, not shell — the live recap loop used
    # exactly these shapes (';', '|', '>', '$?' in the text) and the streak kept resetting.
    check("specs/0069: punctuation INSIDE the quotes is still narration (the live miss)",
          _is_narration_command('Write-Output "unread: specs/, tests/; internal src/ | done > next"')
          and _is_narration_command('Write-Output "SHELL: use $LASTEXITCODE not $?, Stop-Process -Id not -Name."')
          and _is_narration_command('Write-Output "STATUS: files edited: NONE. Identity announced: NO (not asked)."'))
    check("specs/0069: a REAL operator outside the quotes still disqualifies (pipe/chain/redirect/subexpr)",
          not _is_narration_command('Write-Output "a | b" | Set-Content f.txt')
          and not _is_narration_command('Write-Output "done"; Remove-Item b')
          and not _is_narration_command('Write-Output "x; y" > out.txt')
          and not _is_narration_command('Write-Output "$(Get-Content secret.txt)"'))

    # 2. the outcome is registered as an honest gate outcome (never washed to completed / success)
    check("outcomes: 'narration_stall' is a gate outcome, returned as-is (not washed to completed)",
          "narration_stall" in outcomes.GATE_OUTCOMES
          and outcomes.classify("narration_stall", 9) == "narration_stall")

    # 3. flag ON: repeated pure narration -> bounded nudge -> honest 'narration_stall'
    config.GUARD_NARRATION_STALL = True
    r = _agent(_Traj(), _NarrPlanner(), _Reg()).run("review then wait for my next instruction", _ctx())
    check("flag ON: a pure-narration loop ends as 'narration_stall' (not max_steps, not completed)",
          r.terminated == "narration_stall")

    # 3b. specs/0069 end-to-end: the punctuation-heavy narration from the LIVE loop also trips the guard
    r = _agent(_Traj(), _NarrPlanner(
        'Write-Output "WHERE LEFT OFF: ideas delivered; nothing built | user picks section > next"'),
        _Reg()).run("tell me where we left off", _ctx())
    check("flag ON: punctuation-heavy narration (the live-log shape) also ends as 'narration_stall'",
          r.terminated == "narration_stall")

    # 4. flag OFF: byte-identical — the guard never runs, the loop just spends its steps -> 'max_steps'
    config.GUARD_NARRATION_STALL = False
    r = _agent(_Traj(), _NarrPlanner(), _Reg()).run("review then wait", _ctx())
    check("flag OFF: the same loop is NOT narration_stall (guard skipped, byte-identical)",
          r.terminated != "narration_stall")

    # 5. a NON-narration action (a real read) never trips the guard even with the flag on
    config.GUARD_NARRATION_STALL = True
    r = _agent(_Traj(), _NarrPlanner('Get-Content README.md'), _Reg()).run("read the file repeatedly", _ctx())
    check("flag ON: a repeated REAL command (Get-Content) is never flagged as a narration stall",
          r.terminated != "narration_stall")

    for k, v in _saved.items():
        setattr(config, k, v)

    passed, total = sum(_results), len(_results)
    print(f"\nVERDICT: {passed}/{total} {'[OK]' if passed == total else '[FAIL]'}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
