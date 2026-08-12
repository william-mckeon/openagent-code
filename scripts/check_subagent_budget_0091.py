"""
scripts/check_subagent_budget_0091.py

Acceptance harness for specs/0091 — the subagent budget. Two new knobs make a SPAWNED child cheap while the main
agent stays premium: CODE_SUBAGENT_EFFORT (the child's reasoning effort when its caller didn't pin one) and
CODE_SUBAGENT_MAX_STEPS (a smaller child step budget). Dep-free (fake litellm for the src imports; every heavy
collaborator is stubbed so nothing hits a model).

Proves:
  A. runtime.build_agent maps `max_steps` onto the child Agent — None/omitted -> config.MAX_STEPS (byte-identical
     for the main/resume callers), a value -> that value.
  B. subagent.run_subagent fills `effort` from SUBAGENT_EFFORT ONLY when the caller passed None; an explicit
     caller effort (the grounding verifier / guardian) still wins; unset -> None (inherit the global pin); and it
     forwards `config.SUBAGENT_MAX_STEPS or None` as build_agent's max_steps.

    python scripts/check_subagent_budget_0091.py
"""
import os
import sys
import types
import io
import contextlib
import importlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

if "litellm" not in sys.modules:
    _lit = types.ModuleType("litellm")
    _lit.completion = lambda *a, **k: None
    _lit.suppress_debug_info = True
    _lit.modify_params = True
    for _n in ("APIError", "APIConnectionError", "RateLimitError", "Timeout", "BadRequestError",
               "AuthenticationError"):
        setattr(_lit, _n, type(_n, (Exception,), {}))
    sys.modules["litellm"] = _lit

from src import config, runtime, subagent   # noqa: E402
from src.permissions import Permissions      # noqa: E402

_results = []


def check(label, cond):
    _results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


# ---- A. runtime.build_agent: the max_steps -> Agent mapping (the one changed line) --------------------
def _build_agent_capture(max_steps_arg):
    """Call runtime.build_agent with every heavy collaborator stubbed, and return the max_steps the child
    Agent was constructed with. `max_steps_arg` is a sentinel: `"OMIT"` calls build_agent WITHOUT the kwarg
    (the main/resume path), otherwise it's forwarded as max_steps=."""
    saved = {k: getattr(runtime, k) for k in
             ("Model", "active_tools", "make_planner", "openai_schemas", "build_system_prompt",
              "ContextManager", "Registry", "Agent")}
    captured = {}
    try:
        runtime.Model = lambda *a, **k: object()
        runtime.active_tools = lambda: []
        runtime.make_planner = lambda *a, **k: object()
        runtime.openai_schemas = lambda *a, **k: []
        runtime.build_system_prompt = lambda *a, **k: "sys"
        runtime.ContextManager = lambda *a, **k: types.SimpleNamespace(set_pinned=lambda p: None)
        runtime.Registry = lambda t: t

        def _agent(planner, registry, trajectory, max_steps, context_manager):
            captured["max_steps"] = max_steps
            return object()
        runtime.Agent = _agent

        if max_steps_arg == "OMIT":
            runtime.build_agent(trajectory=object())
        else:
            runtime.build_agent(trajectory=object(), max_steps=max_steps_arg)
    finally:
        for k, v in saved.items():
            setattr(runtime, k, v)
    return captured.get("max_steps")


def part_a():
    print("A. runtime.build_agent max_steps mapping")
    check("omitted (main/resume path) -> child Agent gets config.MAX_STEPS (byte-identical)",
          _build_agent_capture("OMIT") == config.MAX_STEPS)
    check("max_steps=None -> child Agent gets config.MAX_STEPS (byte-identical)",
          _build_agent_capture(None) == config.MAX_STEPS)
    check("max_steps=7 -> child Agent gets 7 (the capped child budget)",
          _build_agent_capture(7) == 7)


# ---- B. subagent.run_subagent: effort defaulting + max_steps forwarding -------------------------------
def _run_subagent_capture(caller_effort, sub_effort, sub_max_steps):
    """Set the two config knobs, run run_subagent with everything heavy stubbed, and return the (effort,
    max_steps) that reached build_agent. `caller_effort` is what an explicit caller (grounding/guardian)
    would pass; None = a generic spawn."""
    saved_cfg = {"SUBAGENT_EFFORT": config.SUBAGENT_EFFORT, "SUBAGENT_MAX_STEPS": config.SUBAGENT_MAX_STEPS}
    saved_mod = {k: getattr(subagent, k) for k in ("Trajectory", "make_context", "build_agent", "_classify")}
    captured = {}
    try:
        config.SUBAGENT_EFFORT = sub_effort
        config.SUBAGENT_MAX_STEPS = sub_max_steps

        subagent.Trajectory = lambda *a, **k: types.SimpleNamespace(
            session_id="child", tool_calls=[], end=lambda *aa, **kk: None)
        subagent.make_context = lambda *a, **k: object()
        subagent._classify = lambda *a, **k: "ok"

        def _ba(traj, effort=None, granted_dirs=None, cwd=None, max_steps=None, **kw):
            captured["effort"] = effort
            captured["max_steps"] = max_steps
            return types.SimpleNamespace(
                run=lambda task, ctx: types.SimpleNamespace(final="done", terminated=False))
        subagent.build_agent = _ba

        pctx = types.SimpleNamespace(depth=0, permissions=Permissions("bypass", {}, []),
                                     session_id="parent", cwd=ROOT, verbose=False, traj_dir=None)
        subagent.run_subagent("do a thing", pctx, effort=caller_effort)
    finally:
        config.SUBAGENT_EFFORT = saved_cfg["SUBAGENT_EFFORT"]
        config.SUBAGENT_MAX_STEPS = saved_cfg["SUBAGENT_MAX_STEPS"]
        for k, v in saved_mod.items():
            setattr(subagent, k, v)
    return captured.get("effort", "MISSING"), captured.get("max_steps", "MISSING")


def part_b():
    print("B. run_subagent effort defaulting + max_steps forwarding")
    # generic spawn (caller effort None) + SUBAGENT_EFFORT set -> child runs cheap
    eff, ms = _run_subagent_capture(caller_effort=None, sub_effort="low", sub_max_steps=12)
    check("generic child (caller None) picks up SUBAGENT_EFFORT=low", eff == "low")
    check("generic child forwards SUBAGENT_MAX_STEPS=12 as build_agent max_steps", ms == 12)

    # an EXPLICIT caller effort (grounding verifier / guardian) still wins over SUBAGENT_EFFORT
    eff, _ = _run_subagent_capture(caller_effort="high", sub_effort="low", sub_max_steps=12)
    check("explicit caller effort (e.g. GROUNDING_EFFORT/GUARDIAN_EFFORT) overrides SUBAGENT_EFFORT", eff == "high")

    # unset SUBAGENT_EFFORT -> child effort stays None (inherit the global pin) — byte-identical
    eff, ms = _run_subagent_capture(caller_effort=None, sub_effort="", sub_max_steps=0)
    check("SUBAGENT_EFFORT unset -> child effort None (inherit global pin, byte-identical)", eff is None)
    check("SUBAGENT_MAX_STEPS=0 -> build_agent gets max_steps=None (inherit MAX_STEPS, byte-identical)", ms is None)


# ---- C. the CODE_SUBAGENT_EFFORT env-parse validation gate (adversarial-review catch) ----------------
# The reviewer found Part A/B set config.SUBAGENT_EFFORT DIRECTLY, bypassing the env-parse allowlist — so they
# couldn't catch that a documented-but-invalid value (xhigh/minimal are NOT in the per-role _EFFORTS ladder)
# silently falls to "" and leaves the child on the EXPENSIVE global pin. Here we exercise the ACTUAL config.py
# parse via reload, and lock the .env.example doc so it can't re-promise minimal/xhigh.
def _subagent_effort_doc_block():
    """The contiguous comment block immediately above the CODE_SUBAGENT_EFFORT= line in .env.example."""
    text = open(os.path.join(ROOT, ".env.example"), encoding="utf-8").read().splitlines()
    for i, line in enumerate(text):
        if line.startswith("CODE_SUBAGENT_EFFORT="):
            block = []
            j = i - 1
            while j >= 0 and text[j].lstrip().startswith("#"):
                block.append(text[j])
                j -= 1
            return "\n".join(reversed(block))
    return ""


def part_c():
    print("C. CODE_SUBAGENT_EFFORT env-parse gate + doc truth")
    from src import config as _cfg
    check("the per-role effort ladder is {low,medium,high} (xhigh/minimal are NOT per-role efforts)",
          _cfg._EFFORTS == {"low", "medium", "high"})

    saved_env = os.environ.get("CODE_SUBAGENT_EFFORT")
    try:
        # a documented-but-invalid value must be REJECTED (fall to "") and WARN — not silently keep the pin
        os.environ["CODE_SUBAGENT_EFFORT"] = "xhigh"
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            importlib.reload(_cfg)
        check("CODE_SUBAGENT_EFFORT=xhigh -> SUBAGENT_EFFORT falls to '' (child inherits pin, not the value)",
              _cfg.SUBAGENT_EFFORT == "")
        check("...and it WARNS to stderr (the silent-failure fix; mirrors _env_int)",
              "CODE_SUBAGENT_EFFORT" in buf.getvalue() and "xhigh" in buf.getvalue())

        # a valid per-role effort applies, with NO warning
        os.environ["CODE_SUBAGENT_EFFORT"] = "low"
        buf2 = io.StringIO()
        with contextlib.redirect_stderr(buf2):
            importlib.reload(_cfg)
        check("CODE_SUBAGENT_EFFORT=low -> SUBAGENT_EFFORT='low' (a valid per-role effort applies)",
              _cfg.SUBAGENT_EFFORT == "low")
        check("...a valid value does NOT warn", "CODE_SUBAGENT_EFFORT" not in buf2.getvalue())
    finally:
        if saved_env is None:
            os.environ.pop("CODE_SUBAGENT_EFFORT", None)
        else:
            os.environ["CODE_SUBAGENT_EFFORT"] = saved_env
        with contextlib.redirect_stderr(io.StringIO()):
            importlib.reload(_cfg)

    # doc truth: the .env.example stanza must document the real set and NOT re-promise minimal/xhigh
    doc = _subagent_effort_doc_block()
    check("'.env.example CODE_SUBAGENT_EFFORT doc names the real ladder low|medium|high", "low|medium|high" in doc)
    check("'.env.example CODE_SUBAGENT_EFFORT doc does NOT promise minimal/xhigh as valid values",
          "minimal|low|medium|high|xhigh" not in doc and "|xhigh" not in doc)


def main():
    part_a()
    part_b()
    part_c()
    passed, total = sum(_results), len(_results)
    print(f"\nVERDICT: {passed}/{total} {'[OK]' if passed == total else '[FAIL]'}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
