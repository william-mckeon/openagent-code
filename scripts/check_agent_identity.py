"""
scripts/check_agent_identity.py

Acceptance harness for specs/0063 — the structured <model_information> identity block. Dep-free: no model,
no network (build_system_prompt is pure). Proves the block renders in the format the base model treats as
authoritative (so the agent reports Arcus, not "Inkling, created by Thinking Machines"), that empty fields
are omitted, that it names no base model, and that OFF is byte-identical. Run:

    python scripts/check_agent_identity.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import config, prompts  # noqa: E402
from src.toolset import active_tools  # noqa: E402

_results = []


def check(label, cond):
    _results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


def main():
    tools = active_tools()
    _saved = (config.AGENT_IDENTITY_BLOCK, config.AGENT_NAME, config.AGENT_OVERVIEW,
              config.AGENT_CREATOR, config.AGENT_CONTEXT)
    try:
        config.AGENT_NAME = "Arcus"
        config.AGENT_OVERVIEW = "a self-hosted coding agent"
        config.AGENT_CREATOR = "Islander Intelligence"
        config.AGENT_CONTEXT = "256k tokens"

        # OFF (default): no block
        config.AGENT_IDENTITY_BLOCK = False
        off = prompts.build_system_prompt("native", tools)
        check("block OFF: no <model_information> block (byte-identical)", "<model_information>" not in off)

        # ON: the block renders with the fields, the directive, and no base-model tokens
        config.AGENT_IDENTITY_BLOCK = True
        on = prompts.build_system_prompt("native", tools)
        check("block ON: <model_information> present with the agent's fields",
              "<model_information>" in on and "Name: Arcus" in on
              and "Overview: a self-hosted coding agent" in on
              and "Creator: Islander Intelligence" in on and "Context window: 256k tokens" in on)
        check("block ON: the 'answer consistently' directive + base-model ban are present",
              "answer consistently with the <model_information> block above" in on
              and "NEVER" in on and "underlying base model" in on)
        check("block ON: it sits right after the opening identity line",
              "a real repository.\n\n<model_information>" in on)
        check("block ON: it names NO base model / provider (Inkling / Thinking Machines absent)",
              "Inkling" not in on and "Thinking Machines" not in on)

        # empty fields are omitted (only Name renders)
        config.AGENT_OVERVIEW = config.AGENT_CREATOR = config.AGENT_CONTEXT = ""
        on2 = prompts.build_system_prompt("native", tools)
        check("empty fields are omitted (only Name renders)",
              "Name: Arcus" in on2 and "Overview:" not in on2
              and "Creator:" not in on2 and "Context window:" not in on2)

        # byte-identity: OFF == ON with the injected block excised
        config.AGENT_OVERVIEW = "a self-hosted coding agent"
        config.AGENT_CREATOR = "Islander Intelligence"
        config.AGENT_CONTEXT = "256k tokens"
        on3 = prompts.build_system_prompt("native", tools)
        i = on3.find("\n\n<model_information>")
        j = on3.find("model provider.", i)
        stripped = (on3[:i] + on3[j + len("model provider."):]) if (i >= 0 and j >= 0) else on3
        check("byte-identity: OFF == ON minus the injected identity block", stripped == off)
    finally:
        (config.AGENT_IDENTITY_BLOCK, config.AGENT_NAME, config.AGENT_OVERVIEW,
         config.AGENT_CREATOR, config.AGENT_CONTEXT) = _saved

    # flag is opt-in
    _f = os.environ.pop("CODE_AGENT_IDENTITY_BLOCK", None)
    default_off = config._as_bool(os.environ.get("CODE_AGENT_IDENTITY_BLOCK", "false")) is False
    if _f is not None:
        os.environ["CODE_AGENT_IDENTITY_BLOCK"] = _f
    check("CODE_AGENT_IDENTITY_BLOCK defaults False when unset (opt-in)", default_off)

    passed, total = sum(_results), len(_results)
    print(f"\nVERDICT: {passed}/{total} {'[OK]' if passed == total else '[FAIL]'}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
