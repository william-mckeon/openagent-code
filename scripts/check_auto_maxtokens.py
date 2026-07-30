r"""
scripts/check_auto_maxtokens.py

Acceptance harness for specs/0045 — auto max_tokens (auto context-window resolution + optional per-request
output cap). DEP-FREE: stdlib + src, NEVER litellm (a fake litellm is injected into sys.modules before
`from src import model`; the network resolver path is monkeypatched). Import-time parsing of the `auto`
sentinel is checked in clean subprocesses. Run:

    python scripts/check_auto_maxtokens.py
"""
import os
import sys
import types
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_fake = types.ModuleType("litellm")
_fake.suppress_debug_info = True
_fake.modify_params = True
_fake.completion = lambda **k: None
sys.modules["litellm"] = _fake

from src import config, model, context, trajectory  # noqa: E402

_results = []


def check(label, cond):
    _results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


def _parse(val):
    """Import config in a CLEAN subprocess with CODE_MODEL_MAX_TOKENS=val; return 'AUTO MAX'."""
    env = dict(os.environ)
    env["CODE_MODEL_MAX_TOKENS"] = val
    code = ("import sys; sys.path.insert(0, %r); from src import config; "
            "print(config.MODEL_MAX_TOKENS_AUTO, config.MODEL_MAX_TOKENS)" % ROOT)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)
    return r.stdout.strip()


def main():
    saved = (config.MODEL_MAX_TOKENS, config.COMPACT_HARD_AT_TOKENS, config.SUMMARIZE_INPUT_MAX_TOKENS,
             config.MODEL_MAX_TOKENS_AUTO, config.MODEL_MAX_OUTPUT_TOKENS, config.MODEL_MAX_OUTPUT_TOKENS_AUTO,
             config.OUTPUT_MARGIN_TOKENS, config.API_BASE, config.VERBOSE)
    saved_gmi = getattr(model.litellm, "get_model_info", None)

    # ---- derived-budget regression lock ------------------------------------------------------------
    config._recompute_window_budgets(131072)
    check("_recompute_window_budgets(131072) -> exact pre-0045 triple 131072/119072/96072",
          (config.MODEL_MAX_TOKENS, config.COMPACT_HARD_AT_TOKENS, config.SUMMARIZE_INPUT_MAX_TOKENS)
          == (131072, 119072, 96072))
    config._recompute_window_budgets(40000)
    ok_order = (config.MODEL_MAX_TOKENS >= config.COMPACT_HARD_AT_TOKENS > config.SUMMARIZE_INPUT_MAX_TOKENS > 0)
    check("_recompute_window_budgets(40000): ordering held + 8000 floors honored",
          ok_order and config.SUMMARIZE_INPUT_MAX_TOKENS == 8000 and config.COMPACT_HARD_AT_TOKENS == 28000)

    # ---- import-time parse of the sentinel (clean subprocesses) ------------------------------------
    check("parse: CODE_MODEL_MAX_TOKENS=auto -> AUTO True, fallback 131072", _parse("auto") == "True 131072")
    check("parse: CODE_MODEL_MAX_TOKENS=40000 -> AUTO False, 40000", _parse("40000") == "False 40000")
    check("parse: CODE_MODEL_MAX_TOKENS=garbage -> AUTO False, fallback 131072", _parse("xyz") == "False 131072")

    # ---- output-cap math ---------------------------------------------------------------------------
    config._recompute_window_budgets(131072)
    config.OUTPUT_MARGIN_TOKENS = 4096
    config.MIN_OUTPUT_TOKENS = 512
    msgs = [{"role": "user", "content": "what is zero-based budgeting?"}]
    est = context.estimate_tokens(msgs)

    config.MODEL_MAX_OUTPUT_TOKENS_AUTO, config.MODEL_MAX_OUTPUT_TOKENS = True, 0
    check("output cap auto: window - prompt_estimate - margin",
          model._output_cap(msgs) == max(512, 131072 - est - 4096))
    huge = [{"role": "user", "content": "x" * 10_000_000}]
    check("output cap auto: floored at MIN_OUTPUT_TOKENS for a huge prompt", model._output_cap(huge) == 512)

    config.MODEL_MAX_OUTPUT_TOKENS_AUTO, config.MODEL_MAX_OUTPUT_TOKENS = False, 2000
    check("output cap fixed: positive int sent as-is", model._output_cap(msgs) == 2000)

    config.MODEL_MAX_OUTPUT_TOKENS_AUTO, config.MODEL_MAX_OUTPUT_TOKENS = False, 0
    check("output cap default (unset) -> None (no max_tokens key, byte-identical)", model._output_cap(msgs) is None)

    # ---- resolver: never raises, falls back; no-op when AUTO off -----------------------------------
    config._recompute_window_budgets(131072)
    config.MODEL_MAX_TOKENS_AUTO, config.API_BASE, config.VERBOSE = True, "", False

    def _raise(_m):
        raise RuntimeError("model-info unavailable")
    model.litellm.get_model_info = _raise
    out = model.resolve_model_window()          # must not raise
    check("resolver failure -> swallowed, keeps 131072 fallback", out == 131072 and config.MODEL_MAX_TOKENS == 131072)

    config.MODEL_MAX_TOKENS_AUTO = False
    config._recompute_window_budgets(50000)
    called = {"n": 0}
    model.litellm.get_model_info = lambda m: called.__setitem__("n", called["n"] + 1) or {}
    check("resolver no-op when AUTO off (get_model_info not called, window untouched)",
          model.resolve_model_window() == 50000 and called["n"] == 0)

    # ---- provenance ---------------------------------------------------------------------------------
    check("SCHEMA_VERSION unchanged (0.13.0)", trajectory.Trajectory.SCHEMA_VERSION == "0.13.0")
    fp = config.safety_fingerprint()
    check("auto-max-tokens flags absent from safety_fingerprint",
          "max_output" not in repr(fp).lower() and "output_margin" not in repr(fp).lower())

    # ---- defaults proven against the fallback -------------------------------------------------------
    envs = {k: os.environ.pop(k, None) for k in ("CODE_MODEL_MAX_OUTPUT_TOKENS", "CODE_MODEL_MAX_TOKENS")}
    default_off = (os.environ.get("CODE_MODEL_MAX_OUTPUT_TOKENS", "") == ""
                   and os.environ.get("CODE_MODEL_MAX_TOKENS", "131072") == "131072")
    for k, v in envs.items():
        if v is not None:
            os.environ[k] = v
    check("CODE_MODEL_MAX_OUTPUT_TOKENS + auto sentinel default OFF (opt-in)", default_off)

    # ---- restore -----------------------------------------------------------------------------------
    (config.MODEL_MAX_TOKENS, config.COMPACT_HARD_AT_TOKENS, config.SUMMARIZE_INPUT_MAX_TOKENS,
     config.MODEL_MAX_TOKENS_AUTO, config.MODEL_MAX_OUTPUT_TOKENS, config.MODEL_MAX_OUTPUT_TOKENS_AUTO,
     config.OUTPUT_MARGIN_TOKENS, config.API_BASE, config.VERBOSE) = saved
    if saved_gmi is not None:
        model.litellm.get_model_info = saved_gmi
    elif hasattr(model.litellm, "get_model_info"):
        del model.litellm.get_model_info

    passed, total = sum(_results), len(_results)
    print(f"\nVERDICT: {passed}/{total} {'[OK]' if passed == total else '[FAIL]'}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
