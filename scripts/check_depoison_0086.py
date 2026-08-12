"""
scripts/check_depoison_0086.py

Acceptance harness for specs/0086 — compaction/resume de-poison. Dep-free (no model). Proves drop_narration_noise
strips no-op narration turns + STOP nudges while preserving tool-call<->result pairing and all real work; that a
RESUMED session's history is de-poisoned in __init__ when the flag is on (byte-identical off); and that _compact
feeds the summarizer the FILTERED history. Run:

    python scripts/check_depoison_0086.py
"""
import os
import sys
import json

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import config                                        # noqa: E402
from src.context import ContextManager, drop_narration_noise  # noqa: E402

_results = []


def check(label, cond):
    _results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


def _call(cid, cmd):
    return {"id": cid, "type": "function",
            "function": {"name": "run_command", "arguments": json.dumps({"command": cmd})}}


def narr(i, cmd="Write-Output 'status update'"):
    return {"role": "assistant", "content": "", "tool_calls": [_call(f"n{i}", cmd)]}


def work(i, cmd="Get-Content README.md"):
    return {"role": "assistant", "content": "", "tool_calls": [_call(f"w{i}", cmd)]}


def res(cid, text="ok"):
    return {"role": "tool", "tool_call_id": cid, "content": text}


NUDGE = {"role": "user", "content": "STOP. You have run several side-effect-free narration commands (Write-Output "
                                    "/ echo) that change nothing and make no progress."}
USER = {"role": "user", "content": "review the repo"}


def _pairing_ok(msgs):
    """Every tool result must be preceded (in its group) by an assistant tool_call with the matching id."""
    open_ids = set()
    for m in msgs:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            open_ids = {c["id"] for c in m["tool_calls"]}
        elif m.get("role") == "tool":
            if m.get("tool_call_id") not in open_ids:
                return False
        else:
            open_ids = set()
    return True


class _Model:
    def __init__(self):
        self.last = None

    def summarize(self, msgs):
        self.last = list(msgs)
        return ""          # empty -> compaction always shrinks


class _Traj:
    def log_turn(self, m): pass
    def log_compaction(self, *a): pass


def main():
    _saved = config.COMPACT_DROP_NOISE

    poisoned = [USER, narr(1), res("n1", "status update"), work(2), res("w2", "file"),
                NUDGE, narr(3, "Write-Output 'a'; Write-Output 'b'"), res("n3")]

    # -- drop_narration_noise: strips narration turns + nudges, keeps real work, pairing intact ------------
    cleaned = drop_narration_noise(poisoned)
    kept_cmds = [json.loads(c["function"]["arguments"])["command"]
                 for m in cleaned if m.get("tool_calls") for c in m["tool_calls"]]
    check("drops the narration turns + their results (single- AND multi-statement)",
          all("Write-Output" not in c for c in kept_cmds))
    check("keeps the real work turn (Get-Content) + its result",
          any("Get-Content" in c for c in kept_cmds)
          and any(m.get("tool_call_id") == "w2" for m in cleaned))
    check("drops the STOP nudge user message", NUDGE not in cleaned)
    check("keeps the real user turn", USER in cleaned)
    check("result is tool-call<->result PAIRING-valid", _pairing_ok(cleaned))
    check("expected shape: [user, work-assistant, work-result] (3 msgs)", len(cleaned) == 3)

    # -- a CLEAN history is a strict no-op (byte-identical) -----------------------------------------------
    clean = [USER, work(2), res("w2", "file")]
    check("no-op on a clean history (byte-identical)", drop_narration_noise(clean) == clean)

    # -- ContextManager RESUME de-poison (__init__) -------------------------------------------------------
    config.COMPACT_DROP_NOISE = True
    cm_on = ContextManager("sys", _Model(), _Traj(), compact_at_tokens=0, initial_working=poisoned)
    on_cmds = [json.loads(c["function"]["arguments"])["command"]
               for m in cm_on.working if m.get("tool_calls") for c in m["tool_calls"]]
    check("resume ON: working set is de-poisoned (no Write-Output turns rehydrated)",
          all("Write-Output" not in c for c in on_cmds) and _pairing_ok(cm_on.working))

    config.COMPACT_DROP_NOISE = False
    cm_off = ContextManager("sys", _Model(), _Traj(), compact_at_tokens=0, initial_working=poisoned)
    check("resume OFF: working set keeps the full history (byte-identical)",
          len(cm_off.working) == len(poisoned))

    # -- _compact feeds the summarizer the FILTERED history ----------------------------------------------
    config.COMPACT_DROP_NOISE = True
    mdl = _Model()
    cm = ContextManager("sys", mdl, _Traj(), compact_at_tokens=1, keep_recent=1)
    cm.working = [narr(1), res("n1"), work(2), res("w2", "file"), {"role": "user", "content": "next"}]
    cm._compact()
    summ_cmds = [json.loads(c["function"]["arguments"])["command"]
                 for m in (mdl.last or []) if m.get("tool_calls") for c in m["tool_calls"]]
    check("_compact ON: the summarizer receives the FILTERED old history (no narration)",
          mdl.last is not None and all("Write-Output" not in c for c in summ_cmds)
          and any("Get-Content" in c for c in summ_cmds))

    config.COMPACT_DROP_NOISE = _saved
    passed, total = sum(_results), len(_results)
    print(f"\nVERDICT: {passed}/{total} {'[OK]' if passed == total else '[FAIL]'}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
