"""
scripts/check_advisory_register_0092.py

Acceptance harness for specs/0092 — the advisory / conversational register (CODE_ADVISORY_REGISTER). Dep-free
(fake litellm for the src imports). Proves: armed, the assembled native prompt gains the ADVISORY REGISTER note
and native_tools_note stops pinning "a short final summary" (says "the ANSWER to what was asked"); the note
forbids the exact receipt tells from the Centpilot log (CONFIRMED / === SUMMARY === / Status-Awaiting / a ✓
checklist) and the Write-Output-as-reply habit; and OFF is BYTE-IDENTICAL (native_tools_note returns the exact
pre-0092 literal; build_system_prompt appends no note).

    python scripts/check_advisory_register_0092.py
"""
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

if "litellm" not in sys.modules:
    _lit = types.ModuleType("litellm")
    _lit.completion = lambda *a, **k: None
    for _n in ("APIError", "APIConnectionError", "RateLimitError", "Timeout", "BadRequestError",
               "AuthenticationError"):
        setattr(_lit, _n, type(_n, (Exception,), {}))
    sys.modules["litellm"] = _lit

from src import config, prompts        # noqa: E402
from src.toolset import active_tools   # noqa: E402

_results = []

# The EXACT native_tools_note text before 0092 — the byte-identity anchor for the flag-off path.
_FAKE_TOOLS = [{"name": "read_file"}, {"name": "edit_file"}]
_ORIG_CLOSE = ("When the task is done and verified, reply with a short final summary and no tool calls — that "
               "ends the session.")


def check(label, cond):
    _results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


def main():
    _saved = config.ADVISORY_REGISTER
    try:
        names = ", ".join(t["name"] for t in _FAKE_TOOLS)
        expected_off = (f"You have these tools: {names}. Call them using your tool-calling capability. "
                        + _ORIG_CLOSE)

        # -- OFF: byte-identical -----------------------------------------------------------------------
        config.ADVISORY_REGISTER = False
        note_off = prompts.native_tools_note(_FAKE_TOOLS)
        check("OFF: native_tools_note is byte-identical to the pre-0092 literal (short final summary)",
              note_off == expected_off)
        at = active_tools()
        sys_off = prompts.build_system_prompt("native", at)
        check("OFF: the assembled native prompt has NO advisory note", "ADVISORY REGISTER" not in sys_off)
        check("OFF: the assembled prompt still carries the original 'short final summary' close",
              "a short final summary and no tool calls" in sys_off)

        # -- ON: the register appears, the receipt-pin is gone ------------------------------------------
        config.ADVISORY_REGISTER = True
        note_on = prompts.native_tools_note(_FAKE_TOOLS)
        check("ON: native_tools_note drops the 'short final summary' pin",
              "a short final summary" not in note_on)
        check("ON: native_tools_note says the reply is the ANSWER the user asked for",
              "ANSWER to what was asked" in note_on)
        check("ON: native_tools_note still ends the session on a no-tool-call reply",
              "ends the session" in note_on)

        sys_on = prompts.build_system_prompt("native", at)
        check("ON: the assembled native prompt gains the ADVISORY REGISTER note",
              "ADVISORY REGISTER" in sys_on)

        # -- ON: the note actually closes the failure modes from the log -------------------------------
        # locate the advisory note text for targeted assertions
        adv = sys_on[sys_on.index("ADVISORY REGISTER"):]
        check("ON note: forbids a '- CONFIRMED / verified' receipt line",
              "CONFIRMED" in adv and "verified" in adv)
        check("ON note: forbids a '=== SUMMARY ===' block", "=== SUMMARY ===" in adv)
        check("ON note: forbids a 'Status / Awaiting instruction' line", "Awaiting" in adv)
        check("ON note: forbids a ✓ checklist receipt", "checklist" in adv)
        check("ON note: forbids replying via a printed status line (run_command / Write-Output)",
              "Write-Output" in adv and "not a reply" in adv)
        check("ON note: keeps VERIFY/verified as INTERNAL discipline (vocab bleed fix)",
              "INTERNAL" in adv)
        check("ON note: clarifies 'answer directly' permits the reasoning (not omit it)",
              "lead with the substance" in adv and "omit your reasoning" in adv)
        check("ON note: names the advisory triggers (explain / research / weigh / what do you think)",
              "explain" in adv and "research" in adv and "what do you think" in adv)

        # -- ON stays additive: no base-prompt constant was mutated ------------------------------------
        check("ON is strictly LONGER than OFF (additive note only, base prompt untouched)",
              len(sys_on) > len(sys_off))

        # -- ON but NOT user-facing (a subagent): the register is SUPPRESSED --------------------------------
        # specs/0092 adversarial-review fix: build_system_prompt is the SAME builder for the main agent and for
        # a guardian/grounding/spawn subagent. The register is a user-facing concern, so a subagent (user_facing
        # =False) must NOT carry it, or its terse APPROVE/DENY / GROUNDED-UNGROUNDED verdict contract gets pushed
        # toward prose and can be mis-parsed (a spurious DENY). Flag still ON for all of these.
        sub = prompts.build_system_prompt("native", at, user_facing=False)
        check("ON + subagent (user_facing=False): the ADVISORY note is SUPPRESSED",
              "ADVISORY REGISTER" not in sub)
        check("ON + subagent: the native close reverts to the original 'short final summary' (terse contract safe)",
              "a short final summary and no tool calls" in sub and "ANSWER to what was asked" not in sub)
        check("ON + subagent prompt == main prompt with the flag OFF (register fully absent, byte-identical)",
              sub == sys_off)
        check("ON: native_tools_note(user_facing=False) is the original literal despite the flag",
              prompts.native_tools_note(_FAKE_TOOLS, user_facing=False) == expected_off)
        check("ON: native_tools_note(user_facing=True) is still reshaped (main agent keeps the register)",
              "ANSWER to what was asked" in prompts.native_tools_note(_FAKE_TOOLS, user_facing=True))
    finally:
        config.ADVISORY_REGISTER = _saved

    passed, total = sum(_results), len(_results)
    print(f"\nVERDICT: {passed}/{total} {'[OK]' if passed == total else '[FAIL]'}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
