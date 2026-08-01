r"""
scripts/check_self_preservation.py

Acceptance harness for specs/0050 — CODE_GUARD_SELF_KILL. DEP-FREE (stdlib + src, NEVER litellm). The agent
runs as `python -m src`, so a NAME-based python kill terminates it. Proves such a command is HARD-DENIED in
EVERY permission mode (including bypass) when the flag is on, that kill-by-PID / bare python / a
segment-separated python are NOT flagged, and that flag-off is byte-identical. Run:

    python scripts/check_self_preservation.py
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import config, permissions           # noqa: E402
from src.permissions import Permissions        # noqa: E402

_results = []


def check(label, cond):
    _results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


class _Ctx:
    def __init__(self, cwd, perms=None):
        self.cwd = cwd
        self.permissions = perms
        self.depth = 0
        self.interactive = False
        self.propose_phase = None
        self.approved_paths = set()
        self.propose_graduated = False
        self.manifest = None
        self.session_id = "test"
        self.ask = None


def main():
    saved = {k: getattr(config, k) for k in ("GUARD_SELF_KILL", "HOOKS", "GUARDIAN", "PROPOSE", "EXECPOLICY")}
    config.HOOKS = config.GUARDIAN = config.PROPOSE = config.EXECPOLICY = False
    ws = os.path.realpath(tempfile.mkdtemp(prefix="selfkill-"))

    # ---- the matcher --------------------------------------------------------------------------------
    kills = ["Stop-Process -Name python", "Stop-Process -Name python.exe", "taskkill /F /IM python.exe",
             "pkill python", "killall python", "kill -9 python", "a; Stop-Process -Name python"]
    safe = ["Stop-Process -Id 1234", "taskkill /PID 5678", "python -m http.server 8765",
            "Stop-Process -Name foo; python x", 'python -c "import os"', "Get-ChildItem"]
    check("_is_self_kill: flags every name-based python kill", all(permissions._is_self_kill(c) for c in kills))
    check("_is_self_kill: allows PID-kill / bare python / segment-separated python",
          not any(permissions._is_self_kill(c) for c in safe))

    # ---- flag ON: HARD-denied in EVERY mode, incl. bypass -------------------------------------------
    config.GUARD_SELF_KILL = True
    denied_everywhere = True
    for mode in ("bypass", "default", "acceptEdits", "plan", "propose"):
        p = Permissions(mode, {}, [])
        d = p.decide("run_command", {"command": "Stop-Process -Name python"}, _Ctx(ws, p))
        if d.allowed or "self-preservation" not in (d.reason or ""):
            denied_everywhere = False
    check("flag ON: name-based self-kill is DENIED in every mode (bypass/default/acceptEdits/plan/propose)",
          denied_everywhere)

    pb = Permissions("bypass", {}, [])
    dp = pb.decide("run_command", {"command": "Stop-Process -Id 1234"}, _Ctx(ws, pb))
    check("flag ON: kill-by-PID is NOT blocked by self-preservation (bypass allows it)",
          dp.allowed and "self-preservation" not in (dp.reason or ""))

    # ---- flag OFF: byte-identical (bypass allows the self-kill, unchanged) --------------------------
    config.GUARD_SELF_KILL = False
    do = pb.decide("run_command", {"command": "Stop-Process -Name python"}, _Ctx(ws, pb))
    check("flag OFF: self-kill is NOT blocked (byte-identical — bypass allows as before)",
          do.allowed and "self-preservation" not in (do.reason or ""))

    for k, v in saved.items():
        setattr(config, k, v)

    # ---- default proven against the fallback -------------------------------------------------------
    _env = os.environ.pop("CODE_GUARD_SELF_KILL", None)
    default_off = config._as_bool(os.environ.get("CODE_GUARD_SELF_KILL", "false")) is False
    if _env is not None:
        os.environ["CODE_GUARD_SELF_KILL"] = _env
    check("CODE_GUARD_SELF_KILL defaults False when unset (opt-in)", default_off)

    passed, total = sum(_results), len(_results)
    print(f"\nVERDICT: {passed}/{total} {'[OK]' if passed == total else '[FAIL]'}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
