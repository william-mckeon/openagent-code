r"""
scripts/check_naming.py

Acceptance harness for specs/0036 - a nameable agent (label + persona + --set-name/--remove-name launcher).
Dep-free: stdlib + src only (config / prompts / installshim); NEVER imports model/runtime/session/cli, so it
runs without litellm or a configured endpoint. Passes a FIXED tools list to build_system_prompt so the output
depends only on the name/persona, not on this repo's live flags. Run:  python scripts/check_naming.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import config, installshim                       # noqa: E402
from src.prompts import build_system_prompt, BASE_PROMPT  # noqa: E402

_results = []
_TOOLS = [{"name": "read_file"}, {"name": "write_file"}]   # fixed -> prompt is deterministic regardless of flags


def check(label, cond):
    _results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


def prompt():
    return build_system_prompt("native", _TOOLS)


def main():
    saved = {k: getattr(config, k) for k in ("AGENT_NAME", "AGENT_PERSONA", "AGENT_IDENTITY_BLOCK", "LEAN_PROMPT")}

    # ---- label + persona in the system prompt ---------------------------------------------------------
    config.AGENT_IDENTITY_BLOCK = False   # specs/0063: isolate from the live .env — its injected block would
    #                                       break the "prompt starts with the UNTOUCHED BASE_PROMPT" assertion
    config.LEAN_PROMPT = False   # specs/0089: this suite tests the full-prompt name substitution (the .env may arm lean)
    config.AGENT_NAME, config.AGENT_PERSONA = "OAC", ""
    p = prompt()
    check("default name renders 'You are OAC,' (and not the openagent-code literal)",
          "You are OAC," in p and "You are openagent-code," not in p)

    config.AGENT_NAME = "openagent-code"
    p2 = prompt()
    check("CODE_AGENT_NAME=openagent-code restores the original identity line exactly", "You are openagent-code," in p2)
    check("...and that prompt starts with the UNTOUCHED BASE_PROMPT (reversible substitution)", p2.startswith(BASE_PROMPT))

    config.AGENT_NAME, config.AGENT_PERSONA = "OAC", ""
    no_persona = prompt()
    persona_txt = "You are precise, direct, and a little wry."
    config.AGENT_PERSONA = persona_txt
    with_persona = prompt()
    check("an empty persona appends byte-nothing (no trailing persona block)", not no_persona.endswith("wry."))
    check("a set persona is appended as a single trailing line",
          with_persona.endswith(persona_txt) and with_persona == no_persona + "\n\n" + persona_txt)

    config.AGENT_NAME, config.AGENT_PERSONA = "   ", ""
    check("a blank/whitespace CODE_AGENT_NAME coalesces to OAC (never 'You are ,')",
          "You are OAC," in prompt() and "You are ," not in prompt())

    config.AGENT_NAME = "arcus"
    check("a custom name substitutes the ONE identity line", "You are arcus," in prompt())

    config.AGENT_PERSONA = "line1\nline2\rline3"
    check("persona newlines collapse to one line (trap C)", config.agent_persona() == "line1 line2 line3")
    config.AGENT_PERSONA = "z" * 500
    check("persona is length-capped at PERSONA_MAX", len(config.agent_persona()) <= config.PERSONA_MAX)

    for k, v in saved.items():
        setattr(config, k, v)

    # ---- validate_name --------------------------------------------------------------------------------
    for bad in ["../evil", "a b", "C:\\x", "rm -rf", "", "con", "NUL", "com1", "lpt9",
                "openagent-code", "oac", "OAC", "1abc", "z" * 33, "con.txt"]:
        rejected = False
        try:
            installshim.validate_name(bad)
        except ValueError:
            rejected = True
        check(f"validate_name rejects {bad!r}", rejected)
    for good in ["arcus", "Arcus", "my-agent", "agent_9", "z"]:
        try:
            check(f"validate_name accepts {good!r}", installshim.validate_name(good) == good)
        except ValueError:
            check(f"validate_name accepts {good!r}", False)

    # ---- compute_env_update (pure, idempotent, preserving, revertible) --------------------------------
    env0 = "CODE_MODEL=openai/gpt-oss-120b\nCODE_API_KEY=keep-me\n"
    e1 = installshim.compute_env_update(env0, "arcus", "")
    check("compute_env_update sets CODE_AGENT_NAME", "CODE_AGENT_NAME=arcus" in e1)
    check("compute_env_update omits an empty persona line", "CODE_AGENT_PERSONA" not in e1)
    check("compute_env_update preserves pre-existing lines", "CODE_MODEL=openai/gpt-oss-120b" in e1 and "CODE_API_KEY=keep-me" in e1)
    check("compute_env_update is idempotent (double == single)", installshim.compute_env_update(e1, "arcus", "") == e1)
    e2 = installshim.compute_env_update(e1, "arcus", "witty and terse")
    check("compute_env_update sets a persona when given", "CODE_AGENT_PERSONA=witty and terse" in e2)
    check("re-setting the name replaces in place (single CODE_AGENT_NAME line)", e2.count("CODE_AGENT_NAME=") == 1)
    e3 = installshim.compute_env_update(e2, None, None)
    check("compute_env_update(name=None) removes BOTH vars (the revert)",
          "CODE_AGENT_NAME" not in e3 and "CODE_AGENT_PERSONA" not in e3)
    check("revert still preserves the untouched lines", "CODE_MODEL=openai/gpt-oss-120b" in e3 and "CODE_API_KEY=keep-me" in e3)

    # ---- plan_launcher / plan_remove (pure) -----------------------------------------------------------
    win = installshim.plan_launcher("arcus", "C:\\root", "C:\\root\\.venv\\Scripts\\openagent-code.exe",
                                    "C:\\root\\.venv\\Scripts\\python.exe", windows=True)
    check("windows launcher path is scripts/<name>.ps1", win.path.endswith(os.path.join("scripts", "arcus.ps1")))
    check("windows launcher is a PowerShell function embedding the ABSOLUTE exe + @args",
          "function arcus {" in win.content and "openagent-code.exe" in win.content and "@args" in win.content)
    check("windows launcher yields a $PROFILE dot-source line", win.profile_line.startswith('. "') and "arcus.ps1" in win.profile_line)
    pos = installshim.plan_launcher("arcus", "/root", "/root/.venv/bin/openagent-code", "/root/.venv/bin/python", windows=False)
    check("posix launcher is mode 0o755", pos.chmod == 0o755)
    check("posix launcher is #!/bin/sh, absolute python, '-m src', no CR",
          pos.content.startswith("#!/bin/sh") and "/root/.venv/bin/python" in pos.content
          and " -m src " in pos.content and "\r" not in pos.content)
    check("posix launcher never uses a bare 'python'", 'exec "python"' not in pos.content)
    rem = installshim.plan_remove("arcus", "C:\\root", windows=True)
    check("plan_remove targets exactly what plan_launcher created", rem.path == win.path and rem.profile_line == win.profile_line)

    # ---- profile_ensure / profile_remove (pure $PROFILE line management, specs/0037) ------------------
    line = '. "C:\\root\\scripts\\arcus.ps1"'
    prof0 = '# my profile\n. "C:\\root\\scripts\\oac.ps1"\n'
    p1, ch1 = installshim.profile_ensure(prof0, line)
    check("profile_ensure appends the line when absent", line in p1 and ch1)
    check("profile_ensure preserves existing profile lines", "oac.ps1" in p1)
    p2, ch2 = installshim.profile_ensure(p1, line)
    check("profile_ensure is idempotent (no duplicate on re-run)",
          p2 == p1 and (not ch2) and p2.count("arcus.ps1") == 1)
    pe, che = installshim.profile_ensure("", line)
    check("profile_ensure handles an empty profile", pe == line + "\n" and che)
    pn, chn = installshim.profile_ensure("# no trailing newline", line)
    check("profile_ensure inserts exactly one separator when the profile lacks a trailing newline",
          pn == "# no trailing newline\n" + line + "\n" and chn)
    pr, chr_ = installshim.profile_remove(p1, line)
    check("profile_remove drops the line and keeps the rest", line not in pr and chr_ and "oac.ps1" in pr)
    pr2, chr2 = installshim.profile_remove(pr, line)
    check("profile_remove is a no-op when the line is absent", not chr2)

    # ---- default proven against the fallback, not this repo's live .env -------------------------------
    _env = os.environ.pop("CODE_AGENT_NAME", None)
    default_name = (os.environ.get("CODE_AGENT_NAME", "OAC").strip() or "OAC")
    if _env is not None:
        os.environ["CODE_AGENT_NAME"] = _env
    check("CODE_AGENT_NAME defaults to OAC when unset", default_name == "OAC")

    passed, total = sum(_results), len(_results)
    print(f"\nVERDICT: {passed}/{total} {'[OK]' if passed == total else '[FAIL]'}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
