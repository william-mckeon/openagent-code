"""
scripts/check_modelio_0075.py

Acceptance harness for specs/0075 — model-io / import-robustness fixes. Dep-free: a FAKE litellm is injected
before importing src.model (which imports litellm at module top), like check_stream. Run:

    python scripts/check_modelio_0075.py
"""
import os
import io
import sys
import types
import contextlib
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# fake litellm BEFORE importing src.model / src.cli (dep-free)
_fake = types.ModuleType("litellm")
_fake.suppress_debug_info = True
_fake.modify_params = True
_fake.completion = lambda **k: None
sys.modules["litellm"] = _fake

from src import config, model, prompts, cli   # noqa: E402

_results = []


def check(label, cond):
    _results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


def main():
    # #1 config: a malformed CODE_* value falls back instead of raising at import
    os.environ["CODE_TEST_BADINT"] = "abc"
    os.environ["CODE_TEST_BADFLOAT"] = "x.y"
    check("#1 _env_int falls back on a bad value; parses a valid one; uses default when unset",
          config._env_int("CODE_TEST_BADINT", "7") == 7
          and config._env_int("CODE_TEST_UNSET", "5") == 5)
    check("#1 _env_float falls back on a bad value; parses a valid one",
          config._env_float("CODE_TEST_BADFLOAT", "2.5") == 2.5
          and config._env_float("CODE_TEST_UNSET", "1.5") == 1.5)

    # #2 _non_retryable: a rate-limit/throttle is RETRYABLE; a real overflow / bad request is not
    class RateLimitError(Exception): pass
    class ThrottlingException(Exception): pass
    class ContextWindowExceededError(Exception): pass
    class BadRequestError(Exception): pass
    check("#2 a RateLimitError (Bedrock 'too many tokens' throttle) is RETRYABLE (backoff, not raise)",
          model._non_retryable(RateLimitError("Too many tokens, please wait before trying again.")) is False
          and model._non_retryable(ThrottlingException("rate exceeded")) is False)
    check("#2 a real context-window overflow / bad request is still non-retryable",
          model._non_retryable(ContextWindowExceededError("maximum context length exceeded")) is True
          and model._non_retryable(BadRequestError("bad request")) is True)

    # #5 _assemble_stream: the dim style is reset even if the stream RAISES mid-way
    def _raising():
        yield SimpleNamespace(
            choices=[SimpleNamespace(delta=SimpleNamespace(content=None, reasoning_content="thinking",
                                                           tool_calls=None), finish_reason=None)], usage=None)
        raise RuntimeError("stream died mid-way")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        try:
            model._assemble_stream(_raising(), show_reasoning=True)
        except RuntimeError:
            pass
    check("#5 show_reasoning resets the dim style (\\x1b[0m) even when the stream raises mid-way",
          "\x1b[0m" in buf.getvalue())

    # #3 prompts: the WEB note no longer CLOBBERS the WORKDIR pin (note += , not note =)
    _wd = config.WORKDIR_PROMPT
    config.WORKDIR_PROMPT = True
    p = prompts.build_system_prompt("native", [{"name": "read_file"}, {"name": "web_search"}], cwd="C:/ws")
    config.WORKDIR_PROMPT = _wd
    check("#3 the WORKING DIRECTORY pin survives when a web tool is active (WEB note appends, not overwrites)",
          "WORKING DIRECTORY: your workspace is C:/ws" in p and "WEB:" in p)

    # #6 reasoning_pin: a CUSTOM param overrides the ladder regardless of value shape
    _pp, _pv = config.REASONING_PARAM, config.REASONING_VALUE
    config.REASONING_PARAM, config.REASONING_VALUE = "thinking_budget", "high"
    check("#6 a custom reasoning param + a ladder-shaped value STILL overrides the ladder",
          config.reasoning_pin_overrides_ladder() is True)
    config.REASONING_PARAM, config.REASONING_VALUE = "reasoning_effort", "high"
    check("#6 the standard param + a ladder value does NOT override (adaptive can represent it)",
          config.reasoning_pin_overrides_ladder() is False)
    config.REASONING_PARAM, config.REASONING_VALUE = _pp, _pv

    # #4 cli --warmup: a bad value is a usage error (SystemExit 2), not an uncaught ValueError traceback
    try:
        cli._parse_flags(["--warmup", "abc"])
        bad_ok = False
    except SystemExit as e:
        bad_ok = (e.code == 2)
    check("#4 --warmup with a non-numeric value exits with a usage error (SystemExit 2), not a ValueError",
          bad_ok)
    cli._parse_flags(["--warmup", "1.5"])
    check("#4 a valid --warmup still parses", config.WARMUP_BUDGET == 1.5)

    passed, total = sum(_results), len(_results)
    print(f"\nVERDICT: {passed}/{total} {'[OK]' if passed == total else '[FAIL]'}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
