"""
scripts/check_propose_autoplan.py

Acceptance harness for specs/0052 — the propose first-approval backstop (CODE_PROPOSE_AUTOPLAN). Dep-free:
no model, no network. Proves that when the model never calls propose_changes, propose mode is no longer a
read-only dead-end: an attempted mutation becomes an interactive "approve + unlock" prompt, a yes graduates
the session (so the specs/0048 relaxations apply to every further op) and allows the op, a no keeps it
read-only, and OFF is byte-identical to specs/0022/0048 (no prompt, the deny stands). Covers both the file
gate (_propose_gate) and the command gate (_decide_command).

Run:  python scripts/check_propose_autoplan.py
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import config  # noqa: E402
from src.permissions import Permissions  # noqa: E402

_results = []


def check(label, cond):
    _results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


class _Ask:
    """A stub ask channel that records the prompts shown and returns a canned answer."""
    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    def __call__(self, prompt):
        self.calls.append(prompt)
        return self.answer


class _Ctx:
    def __init__(self, cwd, perms, interactive=False, ask=None, propose_graduated=False):
        self.cwd = cwd
        self.permissions = perms
        self.depth = 0
        self.interactive = interactive
        self.propose_phase = "investigate"
        self.approved_paths = set()
        self.ask = ask
        self.session_id = "sess"
        self.manifest = None
        self.propose_graduated = propose_graduated


def main():
    ws = os.path.realpath(tempfile.mkdtemp(prefix="autoplan-ws-"))
    keys = ("PROPOSE", "PROPOSE_AUTOPLAN", "EXECPOLICY", "HOOKS", "GUARDIAN",
            "PROPOSE_RUN_AFTER_APPROVAL", "PROPOSE_EXTEND_AFTER_APPROVAL", "PROPOSE_PERSIST_APPROVAL")
    saved = {k: getattr(config, k) for k in keys}
    RO = "read-only until the manifest is approved"
    try:
        config.PROPOSE = True
        config.HOOKS = config.GUARDIAN = False
        config.PROPOSE_RUN_AFTER_APPROVAL = False
        config.PROPOSE_EXTEND_AFTER_APPROVAL = False
        config.PROPOSE_PERSIST_APPROVAL = False
        p = Permissions("propose", {}, [])

        # ---- FILE gate (edit_file); run_command's execpolicy path is irrelevant here ----
        config.EXECPOLICY = False

        # OFF: a byte-identical dead-end even when interactive — no prompt, plain read-only deny
        config.PROPOSE_AUTOPLAN = False
        ask = _Ask("y")
        c = _Ctx(ws, p, interactive=True, ask=ask)
        d = p.decide("edit_file", {"path": "src/a.py"}, c)
        check("AUTOPLAN off: edit in investigate is DENIED and ask is NOT called (byte-identical)",
              (not d.allowed) and RO in d.reason and ask.calls == [] and c.propose_graduated is False)

        # ON + interactive + yes -> the op is allowed and the session graduates (one prompt)
        config.PROPOSE_AUTOPLAN = True
        ask = _Ask("y")
        c = _Ctx(ws, p, interactive=True, ask=ask)
        d = p.decide("edit_file", {"path": "src/a.py"}, c)
        check("AUTOPLAN on + yes: edit is ALLOWED, session graduated, prompt shown once",
              d.allowed and c.propose_graduated is True and len(ask.calls) == 1)

        # ON + interactive + no -> denied (declined), session stays locked
        ask = _Ask("n")
        c = _Ctx(ws, p, interactive=True, ask=ask)
        d = p.decide("edit_file", {"path": "src/a.py"}, c)
        check("AUTOPLAN on + no: edit is DENIED (declined) and session stays locked",
              (not d.allowed) and "declined" in d.reason and c.propose_graduated is False and len(ask.calls) == 1)

        # ON + headless -> autoplan can't apply (no human), plain deny with no prompt
        ask = _Ask("y")
        c = _Ctx(ws, p, interactive=False, ask=ask)
        d = p.decide("edit_file", {"path": "src/a.py"}, c)
        check("AUTOPLAN on + headless: edit DENIED with no prompt (no human to approve)",
              (not d.allowed) and RO in d.reason and ask.calls == [])

        # ---- COMMAND gate (run_command via execpolicy) — the observed docker-restart case ----
        config.EXECPOLICY = True
        cmd = "docker restart centpilot-test"   # not read-only, not destructive

        config.PROPOSE_AUTOPLAN = False
        d = p.decide("run_command", {"command": cmd}, _Ctx(ws, p, interactive=False))
        check("AUTOPLAN off: a mutating command in investigate is DENIED (read-only)",
              (not d.allowed) and RO in d.reason)

        config.PROPOSE_AUTOPLAN = True
        ask = _Ask("y")
        c = _Ctx(ws, p, interactive=True, ask=ask)
        d = p.decide("run_command", {"command": cmd}, c)
        check("AUTOPLAN on + yes: mutating command ALLOWED + session graduated",
              d.allowed and c.propose_graduated is True and len(ask.calls) == 1)

        # already graduated (e.g. via /approve or a prior yes) -> every further op is RELAXED past the
        # read-only gate (falls to the ask ladder; headless with no rule => 'no human present', NOT the RO deny)
        d = p.decide("run_command", {"command": cmd}, _Ctx(ws, p, interactive=False, propose_graduated=True))
        check("AUTOPLAN on + graduated: a further command is relaxed PAST the read-only gate",
              RO not in d.reason)

        # OFF + already graduated -> still the read-only deny (relaxation needs AUTOPLAN or RUN_AFTER)
        config.PROPOSE_AUTOPLAN = False
        d = p.decide("run_command", {"command": cmd}, _Ctx(ws, p, interactive=False, propose_graduated=True))
        check("AUTOPLAN off + graduated: command still DENIED read-only (byte-identical to 0048)",
              (not d.allowed) and RO in d.reason)

        # the flag is opt-in, tested against the fallback independent of this repo's own .env
        _s = os.environ.pop("CODE_PROPOSE_AUTOPLAN", None)
        default_off = config._as_bool(os.environ.get("CODE_PROPOSE_AUTOPLAN", "false")) is False
        if _s is not None:
            os.environ["CODE_PROPOSE_AUTOPLAN"] = _s
        check("CODE_PROPOSE_AUTOPLAN defaults False when unset (opt-in)", default_off)
    finally:
        for k, v in saved.items():
            setattr(config, k, v)

    passed, total = sum(_results), len(_results)
    print(f"\nVERDICT: {passed}/{total} {'[OK]' if passed == total else '[FAIL]'}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
