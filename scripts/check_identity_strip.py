"""
scripts/check_identity_strip.py

Acceptance harness for specs/0068 — the volunteered-identity strip, the structural backstop to the 0066 prompt
scoping. Dep-free (pure string function + a direct _finish call, no model/network). Proves: a volunteered
"I am {name}, created by …" is removed from a normal answer; a real identity QUESTION disarms the strip; mixed
lines keep their substantive content; the answer is never blanked; and the flag OFF path is byte-identical
(the answer is returned untouched). Run:

    python scripts/check_identity_strip.py
"""
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import config, prompts        # noqa: E402
from src.agent import Agent, RunResult  # noqa: E402

_results = []


def check(label, cond):
    _results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


NAME = "Arcus"
CREATOR = "Islander Intelligence"


def strip(text, request=""):
    return prompts.strip_volunteered_identity(text, request, NAME)


def main():
    # 1. an "**Identity:**" line inside a multi-line report is removed, the surrounding report kept
    s = strip("## Review\nThe code is clean and well-tested.\n\n"
              "**Identity:** I am Arcus, created by Islander Intelligence.\n\nLet me know what to do next.")
    check("an '**Identity:**' line inside a report is removed, the report kept",
          "The code is clean" in s and "Let me know what to do next." in s and CREATOR not in s)

    # 2. a trailing "Also — I am Arcus, created by …" is removed, the real answer preserved
    s = strip("Here is the review of your repo. It looks solid.\n\nAlso — I am Arcus, created by Islander Intelligence.")
    check("a trailing 'Also — I am Arcus, created by …' is stripped, the answer kept",
          "Here is the review of your repo. It looks solid." in s and CREATOR not in s)

    # 3. a MIXED line keeps its substantive content (only the self-intro sentence goes)
    s = strip("**Identity:** I am Arcus, created by Islander Intelligence. I did not read every file.")
    check("a mixed line keeps its real content, drops only the self-intro",
          "I did not read every file." in s and CREATOR not in s)

    # 4. an identity QUESTION disarms the strip (the agent must be able to answer it)
    for q in ("who are you?", "what model are you?", "who created you", "what's your name?",
              "tell me about yourself", "introduce yourself"):
        keep = strip("I am Arcus, created by Islander Intelligence.", q)
        check(f"asked identity ({q!r}) -> answer is NOT stripped", CREATOR in keep)

    # 5. _asks_identity discriminates a real identity question from an ordinary task
    check("_asks_identity: identity questions match; an ordinary task does not",
          prompts._asks_identity("who made you?") and prompts._asks_identity("which model is this")
          and not prompts._asks_identity("build me a portfolio website")
          and not prompts._asks_identity("review this folder file by file"))

    # 6. an ordinary answer with no volunteered identity is returned unchanged
    plain = "The build passes. Two tests cover the new gate. Nothing else changed."
    check("a clean answer (no volunteered identity) is unchanged", strip(plain) == plain)

    # 7. never blanks the whole answer, and no-name / empty text are safe
    only = "I am Arcus, created by Islander Intelligence."
    check("an answer that is ONLY the identity line is not blanked (original kept)", strip(only) == only)
    check("empty text / no name are returned as-is (no crash)",
          prompts.strip_volunteered_identity("", "", NAME) == ""
          and prompts.strip_volunteered_identity("I am Arcus, made by X.", "", "") == "I am Arcus, made by X.")

    # 8. wired into _finish, gated by the flag (byte-identical when off)
    _saved = config.STRIP_VOLUNTEERED_IDENTITY, config.AGENT_NAME
    config.AGENT_NAME = NAME
    ans = "Done — the fix is in place.\n\nAlso — I am Arcus, created by Islander Intelligence."
    ctx = types.SimpleNamespace(request="fix the bug")
    ag = Agent.__new__(Agent)          # bypass __init__: _finish only needs these two attrs + the flag
    ag._effort_policy = None
    ag._escalated = False

    # opt-in default: test the DEFAULT (env var UNSET), not the live config — the operator's .env may have
    # flipped it true (the recurring test-isolation lesson: a harness must not read the ambient flipped flag).
    _f = os.environ.pop("CODE_STRIP_VOLUNTEERED_IDENTITY", None)
    check("CODE_STRIP_VOLUNTEERED_IDENTITY defaults False when unset (opt-in)",
          config._as_bool(os.environ.get("CODE_STRIP_VOLUNTEERED_IDENTITY", "false")) is False)
    if _f is not None:
        os.environ["CODE_STRIP_VOLUNTEERED_IDENTITY"] = _f
    config.STRIP_VOLUNTEERED_IDENTITY = False
    off = ag._finish(ctx, ans, "final", 1)
    check("_finish flag OFF: the answer (with the volunteered tail) is byte-identical",
          isinstance(off, RunResult) and off.final == ans)
    config.STRIP_VOLUNTEERED_IDENTITY = True
    on = ag._finish(ctx, ans, "final", 1)
    check("_finish flag ON: the volunteered tail is stripped, the answer kept",
          "Done — the fix is in place." in on.final and CREATOR not in on.final)
    ctx_ask = types.SimpleNamespace(request="who created you?")
    on_ask = ag._finish(ctx_ask, ans, "final", 1)
    check("_finish flag ON but the user ASKED identity: not stripped", CREATOR in on_ask.final)
    config.STRIP_VOLUNTEERED_IDENTITY, config.AGENT_NAME = _saved

    passed, total = sum(_results), len(_results)
    print(f"\nVERDICT: {passed}/{total} {'[OK]' if passed == total else '[FAIL]'}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
