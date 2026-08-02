"""
scripts/check_prompt_hygiene.py

Acceptance harness for specs/0051 — the prompt-hygiene note (identity discipline + anti-argument +
propose-recovery + service-up honesty) and the extended PowerShell shell-hint rules (head/tail, $?, tree).
Checked WITHOUT a model or a network: build_system_prompt / build_env_context are pure. Run:

    python scripts/check_prompt_hygiene.py

Exits 0 only if every check holds.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import config, envcontext, prompts  # noqa: E402
from src.toolset import active_tools  # noqa: E402

_results = []


def check(label, cond):
    _results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


def main():
    tools = active_tools()
    _saved = config.PROMPT_HYGIENE
    try:
        # 1. OFF (default) -> no HYGIENE note in the system prompt (byte-identical to the pre-flag build)
        config.PROMPT_HYGIENE = False
        off = prompts.build_system_prompt("native", tools)
        check("PROMPT_HYGIENE OFF: no HYGIENE note (byte-identical prompt)", "HYGIENE:" not in off)

        # 2. ON -> the note is present and carries all four rules
        config.PROMPT_HYGIENE = True
        on = prompts.build_system_prompt("native", tools)
        check("PROMPT_HYGIENE ON: HYGIENE note present", "HYGIENE:" in on)
        check("identity rule (persona is a STYLE, not announced/restated)",
              "STYLE to embody" in on and "restating your persona" in on)
        check("anti-argument rule (ADJUST, don't argue about repetition)",
              "never argue" in on.lower() and "ADJUST" in on)
        check("propose-recovery rule (propose before edit/command; don't retry a denied op)",
              "propose_changes BEFORE any edit" in on and "do not retry the raw edit" in on)
        check("service-honesty rule (don't claim up unless actually reached)",
              "unless you actually REACHED it" in on and "2xx" in on)

        # 3. byte-identity: OFF == ON with ONLY the HYGIENE note excised (the flag gates that note and nothing
        #    else). Slice out from the note marker to the start of the next "\n\n" section.
        i = on.find("\n\nHYGIENE:")
        j = on.find("\n\n", i + 2) if i >= 0 else -1
        if i < 0:
            stripped = on
        elif j < 0:
            stripped = on[:i]
        else:
            stripped = on[:i] + on[j:]
        check("OFF prompt == ON prompt minus the HYGIENE note (flag gates ONLY that note)", stripped == off)
    finally:
        config.PROMPT_HYGIENE = _saved

    # 4. the flag is OFF BY DEFAULT (opt-in), tested against the FALLBACK independent of this repo's own .env
    #    (config loads .env at import, so a live ride may have turned it on).
    _s = os.environ.pop("CODE_PROMPT_HYGIENE", None)
    default_off = config._as_bool(os.environ.get("CODE_PROMPT_HYGIENE", "false")) is False
    if _s is not None:
        os.environ["CODE_PROMPT_HYGIENE"] = _s
    check("CODE_PROMPT_HYGIENE defaults False when unset (opt-in)", default_off)

    # 5. shell-hint gaps (specs/0051): the new PS 5.1 rules are present when hints are on (Windows only)
    on_hints = envcontext.build_env_context("/w", shell_hints=True)
    if os.name == "nt":
        check("shell hints ON: head/tail -> Select-Object, $? -> $LASTEXITCODE, tree -Recurse present",
              "Select-Object -First" in on_hints and "$LASTEXITCODE" in on_hints
              and "head" in on_hints and "-Recurse" in on_hints)
    else:
        check("shell hints ON (non-Windows): no PS rules (PowerShell-specific)", "shell rules" not in on_hints)

    passed, total = sum(_results), len(_results)
    print(f"\nVERDICT: {passed}/{total} {'[OK]' if passed == total else '[FAIL]'}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
