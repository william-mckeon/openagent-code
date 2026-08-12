"""
scripts/check_lean_prompt2_0090.py

Acceptance harness for specs/0090 — extending CODE_LEAN_PROMPT to the SECONDARY prompts (tool descriptions, the
build_system_prompt WEB/PROPOSE/SPEC notes, the PowerShell shell rules, the review_repo trailer, the grounding
anti-collapse challenge). Dep-free. Proves the surface shrinks when on, every machine CONTRACT survives, and the
flag OFF is byte-identical (desc_for returns the full description; notes/blocks unchanged).

    python scripts/check_lean_prompt2_0090.py
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

from src import config, tools, grounding, envcontext, prompts   # noqa: E402
from src.toolset import active_tools                            # noqa: E402

_results = []


def check(label, cond):
    _results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


def _tool(name):
    for lst in (tools.TOOLS, tools.WEB_TOOLS, tools.GOAL_TOOLS, tools.PATCH_TOOLS, tools.TODO_TOOLS,
                tools.SPEC_TOOLS, tools.PROPOSE_TOOLS, tools.WORKFLOW_TOOLS, tools.EFFORT_TOOLS):
        for t in lst:
            if t.get("name") == name:
                return t
    return None


def main():
    _saved = {k: getattr(config, k) for k in ("LEAN_PROMPT", "GROUND_ANTI_COLLAPSE")}

    # -- tool descriptions shrink when on, byte-identical off --------------------------------------------
    rr = _tool("review_repo")
    config.LEAN_PROMPT = False
    check("flag OFF: desc_for returns the FULL description (byte-identical)", tools.desc_for(rr) == rr["description"])
    config.LEAN_PROMPT = True
    check("flag ON: a verbose tool (review_repo) description gets shorter", len(tools.desc_for(rr)) < len(rr["description"]))
    check("flag ON: a tool WITHOUT a lean variant (edit_file) is unchanged",
          tools.desc_for(_tool("edit_file")) == _tool("edit_file")["description"])

    # -- machine CONTRACTS preserved in the lean descriptions --------------------------------------------
    ap = tools.desc_for(_tool("apply_patch"))
    check("contract: apply_patch lean keeps the patch envelope markers (*** Begin/End Patch, hunk markers)",
          "*** Begin Patch" in ap and "*** End Patch" in ap and "SEARCH" in ap and "REPLACE" in ap)
    wf = tools.desc_for(_tool("web_fetch"))
    check("contract: web_fetch lean keeps the safety+grounding clause (UNTRUSTED ... NOT instructions; CITE)",
          "UNTRUSTED" in wf.upper() and "NOT instructions" in wf and "CITE" in wf.upper())
    ws = tools.desc_for(_tool("web_search"))
    check("contract: web_search lean keeps the WEAK-citation + untrusted markers",
          "WEAK" in ws.upper() and "untrusted" in ws.lower())
    pu = tools.desc_for(_tool("pursue"))
    check("contract: pursue lean keeps the argv-LIST bar examples", '["npm","test"]' in pu.replace(" ", ""))
    up = tools.desc_for(_tool("update_plan"))
    check("contract: update_plan lean keeps the per-step 'file' completion-gate hook", "'file'" in up or '"file"' in up)
    df = tools.desc_for(_tool("delete_file"))
    check("contract: delete_file lean keeps the NEVER-`rm` steer", "`rm`" in df or "rm`" in df.lower() or "never `rm`" in df.lower())

    # -- full system prompt (native, all tools) shrinks substantially ------------------------------------
    at = active_tools()
    config.LEAN_PROMPT = False
    full = prompts.build_system_prompt("native", at)
    config.LEAN_PROMPT = True
    lean = prompts.build_system_prompt("native", at)
    check("flag ON: the assembled system prompt (native, all tools) is >=30% smaller",
          len(lean) < 0.70 * len(full))
    check("flag OFF then ON differ; OFF keeps the verbose WEB/PROPOSE wording (byte-identical off)",
          "read local code first; use them only when you genuinely need external information" in full
          and "read local code first; use them only when you genuinely need external information" not in lean)

    # -- envcontext PowerShell: lean keeps the footguns, drops the alias catalog -------------------------
    if os.name == "nt":
        lean_ps = envcontext.build_env_context("C:/x", shell_hints=True, lean=True)
        full_ps = envcontext.build_env_context("C:/x", shell_hints=True, lean=False)
        check("PowerShell lean keeps the footguns (; not && ; bare echo HANGS ; Stop-Process -Id ; no 2>&1 ; "
              "curl.exe ; not /dev/null ; New-Item not mkdir -p — the specs/0046 documented traps)",
              "`;`" in lean_ps and "HANGS" in lean_ps and "Stop-Process -Id" in lean_ps and "2>&1" in lean_ps
              and "curl.exe" in lean_ps and "/dev/null" in lean_ps and "New-Item" in lean_ps)
        check("PowerShell lean DROPS the inferable alias catalog (head/tail/cat/Select-String) and is shorter",
              "head" not in lean_ps and "tail" not in lean_ps and "Select-String" not in lean_ps
              and len(lean_ps) < len(full_ps))
    else:
        print("  (skipping the Windows-only PowerShell block checks)")

    # -- grounding anti-collapse challenge: lean is shorter, keeps the RE-SEND intent --------------------
    config.GROUND_ANTI_COLLAPSE = True
    config.LEAN_PROMPT = True
    cl = grounding.challenge(["x"])
    config.LEAN_PROMPT = False
    cf = grounding.challenge(["x"])
    check("grounding challenge: lean is shorter and still says RE-SEND your COMPLETE answer",
          len(cl) < len(cf) and "RE-SEND your COMPLETE answer" in cl)

    for k, v in _saved.items():
        setattr(config, k, v)
    passed, total = sum(_results), len(_results)
    print(f"\nVERDICT: {passed}/{total} {'[OK]' if passed == total else '[FAIL]'}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
