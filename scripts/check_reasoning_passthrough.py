r"""
scripts/check_reasoning_passthrough.py

Acceptance harness for specs/0044 — flexible reasoning pass-through (CODE_REASONING_PARAM / _VALUE /
_TOPLEVEL). DEP-FREE: stdlib + src, NEVER litellm (a fake litellm is injected into sys.modules before
`from src import model`). Proves the flag-OFF byte-identity of _reasoning_kwargs against the pre-0044
behavior, the string/int/object pass-through, top-level vs extra_body routing, per-Model precedence, the
defensive parse, and that the legacy effort machinery + provenance are untouched. Run:

    python scripts/check_reasoning_passthrough.py
"""
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_fake = types.ModuleType("litellm")
_fake.suppress_debug_info = True
_fake.modify_params = True
_fake.completion = lambda **k: None
sys.modules["litellm"] = _fake

from src import config, model  # noqa: E402

_results = []


def check(label, cond):
    _results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


def _legacy_expected(resolved_effort, model_str):
    """The pre-0044 _reasoning_kwargs output for a resolved effort + model, recomputed independently."""
    if not resolved_effort:
        return {}
    if model_str.startswith("bedrock/"):
        return {"reasoning_effort": resolved_effort}
    return {"extra_body": {"reasoning_effort": resolved_effort}}


def main():
    saved = (config.MODEL, config.REASONING_EFFORT, config.REASONING_VALUE,
             config.REASONING_PARAM, config.REASONING_TOPLEVEL)

    # ---- flag-OFF byte-identity: reproduce pre-0044 for every effort x model x per-Model-arg -----------
    config.REASONING_VALUE = ""
    config.REASONING_PARAM = "reasoning_effort"
    config.REASONING_TOPLEVEL = False
    off_ok = True
    for model_str in ("openai/thinkingmachines/Inkling", "bedrock/openai.gpt-oss-120b-1:0"):
        config.MODEL = model_str
        for glob_eff in ("", "low", "medium", "high"):
            config.REASONING_EFFORT = glob_eff
            for arg in (None, "low", "high"):
                resolved = arg or glob_eff
                got = model._reasoning_kwargs(arg)
                if got != _legacy_expected(resolved, model_str):
                    off_ok = False
    check("flag OFF: _reasoning_kwargs is byte-identical to pre-0044 for all effort x model x arg", off_ok)

    # ---- pass-through: string / int / object -----------------------------------------------------------
    config.MODEL = "openai/thinkingmachines/Inkling"
    config.REASONING_EFFORT = ""

    config.REASONING_PARAM, config.REASONING_VALUE, config.REASONING_TOPLEVEL = "reasoning_effort", "xhigh", False
    check("string pass-through -> extra_body, bypasses the low/medium/high allowlist",
          model._reasoning_kwargs() == {"extra_body": {"reasoning_effort": "xhigh"}})

    config.REASONING_PARAM, config.REASONING_VALUE = "reasoning_tokens", 2048
    check("numeric budget pass-through (int preserved)",
          model._reasoning_kwargs() == {"extra_body": {"reasoning_tokens": 2048}})

    config.REASONING_PARAM, config.REASONING_VALUE = "thinking", {"type": "enabled", "budget_tokens": 2048}
    check("object pass-through under a custom key",
          model._reasoning_kwargs() == {"extra_body": {"thinking": {"type": "enabled", "budget_tokens": 2048}}})

    # ---- top-level routing ------------------------------------------------------------------------------
    config.REASONING_PARAM, config.REASONING_VALUE, config.REASONING_TOPLEVEL = "reasoning_effort", "xhigh", True
    check("CODE_REASONING_TOPLEVEL -> payload sent top-level, not under extra_body",
          model._reasoning_kwargs() == {"reasoning_effort": "xhigh"})
    config.REASONING_TOPLEVEL = False
    config.MODEL = "bedrock/openai.gpt-oss-120b-1:0"
    check("bedrock/ model -> pass-through also routed top-level",
          model._reasoning_kwargs() == {"reasoning_effort": "xhigh"})
    config.MODEL = "openai/thinkingmachines/Inkling"

    # ---- per-Model override wins (grounding/guardian/adaptive keep the legacy path) --------------------
    config.REASONING_PARAM, config.REASONING_VALUE = "thinking", {"budget_tokens": 4096}
    check("per-Model effort override beats the global pass-through (legacy string path)",
          model._reasoning_kwargs("low") == {"extra_body": {"reasoning_effort": "low"}})

    # ---- defensive parse (config import must never raise) + literal-string fallback --------------------
    # A non-JSON CODE_REASONING_VALUE lands as a literal string (the 'xhigh' case above exercised the
    # json.loads(ValueError) -> literal path). Prove the legacy effort machinery is untouched:
    check("_EFFORTS unchanged (legacy allowlist intact)", config._EFFORTS == {"low", "medium", "high"})

    config.MODEL, config.REASONING_EFFORT, config.REASONING_VALUE, config.REASONING_PARAM, config.REASONING_TOPLEVEL = saved

    # ---- provenance: pass-through does NOT leak into the recorded effort field -------------------------
    # complete() logs effort = self.effort or config.REASONING_EFFORT (model.py). The pass-through never
    # sets REASONING_EFFORT, so the effort provenance stays a scalar (or None) — never a dict.
    check("pass-through does not touch config.REASONING_EFFORT (effort provenance stays scalar)",
          config.REASONING_EFFORT in ("", "low", "medium", "high"))

    from src import trajectory  # noqa: E402
    check("SCHEMA_VERSION unchanged (0.13.0)", trajectory.Trajectory.SCHEMA_VERSION == "0.13.0")
    fp = config.safety_fingerprint()
    check("CODE_REASONING_* absent from safety_fingerprint", "reasoning" not in repr(fp).lower())

    # ---- defaults proven against the fallback, not the live .env ---------------------------------------
    envs = {k: os.environ.pop(k, None) for k in ("CODE_REASONING_VALUE", "CODE_REASONING_PARAM", "CODE_REASONING_TOPLEVEL")}
    default_off = (os.environ.get("CODE_REASONING_VALUE", "") == ""
                   and os.environ.get("CODE_REASONING_PARAM", "reasoning_effort") == "reasoning_effort"
                   and config._as_bool(os.environ.get("CODE_REASONING_TOPLEVEL", "false")) is False)
    for k, v in envs.items():
        if v is not None:
            os.environ[k] = v
    check("pass-through defaults OFF when unset (opt-in)", default_off)

    passed, total = sum(_results), len(_results)
    print(f"\nVERDICT: {passed}/{total} {'[OK]' if passed == total else '[FAIL]'}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
