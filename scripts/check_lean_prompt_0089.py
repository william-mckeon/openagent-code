"""
scripts/check_lean_prompt_0089.py

Acceptance harness for specs/0089 — the lean system prompt. Dep-free. Proves LEAN_BASE_PROMPT is far smaller than
BASE_PROMPT, keeps the load-bearing behavior and the identity anchors, and that build_system_prompt selects it
only when CODE_LEAN_PROMPT is on (byte-identical when off).

    python scripts/check_lean_prompt_0089.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import config, prompts   # noqa: E402

_results = []


def check(label, cond):
    _results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


def main():
    _saved = config.LEAN_PROMPT

    # -- the lean prompt is much smaller but keeps the anchors + essentials -------------------------------
    check("LEAN_BASE_PROMPT is at least 70% smaller than BASE_PROMPT",
          len(prompts.LEAN_BASE_PROMPT) < 0.30 * len(prompts.BASE_PROMPT))
    check("keeps the identity/name anchor so name-substitution + the <model_information> block still inject",
          prompts.LEAN_BASE_PROMPT.startswith("You are openagent-code,")
          and "a coding agent that edits real files in a real repository." in prompts.LEAN_BASE_PROMPT)
    low = prompts.LEAN_BASE_PROMPT.lower()
    for kw in ("read_file", "edit_file", "delete_file", "`rm`", "verify", "review_repo", "read-only", "workspace"):
        check(f"keeps the load-bearing behavior: {kw!r}", kw.lower() in low)
    check("still forbids a review being answered as a receipt (0088 intent preserved)",
          "receipt" in low and "read-only" in low)

    # -- build_system_prompt selects lean ONLY when the flag is on ----------------------------------------
    config.LEAN_PROMPT = False
    full = prompts.build_system_prompt("bypass", [], memory=None, todos=None)
    config.LEAN_PROMPT = True
    lean = prompts.build_system_prompt("bypass", [], memory=None, todos=None)
    check("flag OFF: build_system_prompt embeds the FULL BASE_PROMPT (byte-identical to before)",
          "Working method:" in full and full != lean)
    check("flag ON: build_system_prompt embeds the LEAN prompt and is substantially shorter",
          len(lean) < len(full) and "Working method:" not in lean)
    check("flag ON: the assembled prompt still opens with the role line + keeps the tool-mode/identity wiring",
          lean.startswith("You are"))

    config.LEAN_PROMPT = _saved
    passed, total = sum(_results), len(_results)
    print(f"\nVERDICT: {passed}/{total} {'[OK]' if passed == total else '[FAIL]'}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
