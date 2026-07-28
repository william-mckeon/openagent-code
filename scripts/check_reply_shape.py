r"""
scripts/check_reply_shape.py

Acceptance harness for specs/0041 F1 — reply-shape precedence (CODE_REPLY_SHAPE). Dep-free: stdlib + src,
NEVER litellm. Proves the flag-off BYTE-IDENTITY (no prompt paragraph, neutral task pin, empty trailer
caveat) and the flag-on behavior (precedence paragraph + per-turn scope in the prompt and the pin, and the
caveat on the digest trailers). Run:  python scripts/check_reply_shape.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import config, workflow                                   # noqa: E402
from src.prompts import build_system_prompt, reply_shape_caveat    # noqa: E402
from src.context import ContextManager                             # noqa: E402

_results = []
_TOOLS = [{"name": "read_file"}]


def check(label, cond):
    _results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


class _Traj:
    def log_turn(self, m): pass
    def log_compaction(self, *a): pass


class _Model:
    def summarize(self, msgs): return "s"


def _pin():
    cm = ContextManager("sys", _Model(), _Traj())
    cm.set_task("do the thing")
    return (cm.pinned_task or {}).get("content", "")


def _digest():
    return workflow.final_digest(["## Phase: p (1 job)\n### a\nfound X"], "prioritize")


def main():
    saved = config.REPLY_SHAPE

    # ---- OFF (default): byte-identical to today ------------------------------------------------------
    config.REPLY_SHAPE = False
    p_off = build_system_prompt("native", _TOOLS)
    check("OFF: no REPLY SHAPE paragraph in the system prompt", "REPLY SHAPE:" not in p_off)
    check("OFF: the task pin uses the neutral 'answer THIS directly'",
          "answer THIS directly" in _pin() and "ONLY instruction in force" not in _pin())
    check("OFF: reply_shape_caveat() is empty", reply_shape_caveat() == "")
    check("OFF: the final_digest trailer carries NO caveat", "UNLESS the user constrained" not in _digest())

    # ---- ON: precedence + per-turn scope -------------------------------------------------------------
    config.REPLY_SHAPE = True
    p_on = build_system_prompt("native", _TOOLS)
    check("ON: the REPLY SHAPE paragraph is present + says it OUTRANKS the tool trailer",
          "REPLY SHAPE:" in p_on and "OUTRANKS" in p_on)
    check("ON: the paragraph scopes the instruction to its own turn",
          "does NOT carry to later turns" in p_on)
    check("ON: the task pin asserts precedence + that an earlier turn's constraint does not apply",
          "ONLY instruction in force this turn" in _pin() and "does NOT apply" in _pin())
    check("ON: reply_shape_caveat() is non-empty", "UNLESS the user constrained" in reply_shape_caveat())
    check("ON: the final_digest trailer carries the caveat", "UNLESS the user constrained" in _digest())

    config.REPLY_SHAPE = saved

    # ---- default proven against the fallback, not this repo's live .env ------------------------------
    _env = os.environ.pop("CODE_REPLY_SHAPE", None)
    default_off = config._as_bool(os.environ.get("CODE_REPLY_SHAPE", "false")) is False
    if _env is not None:
        os.environ["CODE_REPLY_SHAPE"] = _env
    check("CODE_REPLY_SHAPE defaults False when unset (opt-in)", default_off)

    passed, total = sum(_results), len(_results)
    print(f"\nVERDICT: {passed}/{total} {'[OK]' if passed == total else '[FAIL]'}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
