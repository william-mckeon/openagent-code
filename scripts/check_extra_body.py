r"""
scripts/check_extra_body.py

Acceptance harness for specs/0049 — CODE_EXTRA_BODY (merge operator params into the request extra_body).
DEP-FREE: stdlib + src, NEVER litellm (a fake litellm is injected into sys.modules before `from src import
model`). Proves the flag-off byte-identity, the merge into _params(), reasoning-wins-on-conflict, and the
import-time JSON parse (in clean subprocesses). Run:  python scripts/check_extra_body.py
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

from src import config, model  # noqa: E402

_results = []


def check(label, cond):
    _results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


def _parse(val):
    """Import config in a CLEAN subprocess with CODE_EXTRA_BODY=val; return repr(config.EXTRA_BODY)."""
    env = dict(os.environ)
    env["CODE_EXTRA_BODY"] = val
    code = ("import sys; sys.path.insert(0, %r); from src import config; print(repr(config.EXTRA_BODY))" % ROOT)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)
    return r.stdout.strip()


def main():
    saved = (config.EXTRA_BODY, config.MODEL, config.REASONING_VALUE, config.REASONING_PARAM,
             config.REASONING_TOPLEVEL, config.REASONING_EFFORT)
    m = model.Model(None, None)
    config.REASONING_VALUE = ""
    config.REASONING_EFFORT = ""
    config.REASONING_PARAM = "reasoning_effort"
    config.REASONING_TOPLEVEL = False
    config.MODEL = "openai/thinkingmachines/Inkling"

    # ---- flag OFF (default {}): no extra_body when there is no reasoning either (byte-identical) --------
    config.EXTRA_BODY = {}
    check("flag OFF: _params() adds NO extra_body key (byte-identical)", "extra_body" not in m._params())

    # ---- flag ON, no reasoning: EXTRA_BODY becomes the extra_body --------------------------------------
    config.EXTRA_BODY = {"separate_reasoning": True}
    check("flag ON, no reasoning: extra_body == the operator dict",
          m._params().get("extra_body") == {"separate_reasoning": True})

    # ---- merged ALONGSIDE the reasoning pass-through (both keys present) --------------------------------
    config.REASONING_VALUE = "xhigh"
    config.EXTRA_BODY = {"separate_reasoning": True}
    check("merge: reasoning_effort (pass-through) + separate_reasoning (extra_body) coexist",
          m._params().get("extra_body") == {"reasoning_effort": "xhigh", "separate_reasoning": True})

    # ---- key collision: the dedicated reasoning knob WINS ----------------------------------------------
    config.EXTRA_BODY = {"reasoning_effort": "low", "separate_reasoning": True}
    eb = m._params().get("extra_body")
    check("collision: the reasoning knob wins over CODE_EXTRA_BODY's same key",
          eb.get("reasoning_effort") == "xhigh" and eb.get("separate_reasoning") is True)

    # ---- bedrock: reasoning stays TOP-LEVEL, EXTRA_BODY still lands in extra_body -----------------------
    config.MODEL = "bedrock/openai.gpt-oss-120b-1:0"
    config.REASONING_VALUE = ""
    config.REASONING_EFFORT = "high"
    config.EXTRA_BODY = {"foo": 1}
    kw = m._params()
    check("bedrock: reasoning_effort stays top-level AND CODE_EXTRA_BODY -> extra_body",
          kw.get("reasoning_effort") == "high" and kw.get("extra_body") == {"foo": 1})

    (config.EXTRA_BODY, config.MODEL, config.REASONING_VALUE, config.REASONING_PARAM,
     config.REASONING_TOPLEVEL, config.REASONING_EFFORT) = saved

    # ---- import-time JSON parse (clean subprocesses) ---------------------------------------------------
    check("parse: a JSON object -> the dict", _parse('{"separate_reasoning": true}') == "{'separate_reasoning': True}")
    check("parse: non-object JSON (a list) -> {} (only a dict is accepted)", _parse("[1,2]") == "{}")
    check("parse: garbage -> {} (never raises at import)", _parse("not json") == "{}")

    _env = os.environ.pop("CODE_EXTRA_BODY", None)
    default_empty = (os.environ.get("CODE_EXTRA_BODY", "") == "")
    if _env is not None:
        os.environ["CODE_EXTRA_BODY"] = _env
    check("CODE_EXTRA_BODY defaults empty ({}) when unset (opt-in)", default_empty and _parse("") == "{}")

    passed, total = sum(_results), len(_results)
    print(f"\nVERDICT: {passed}/{total} {'[OK]' if passed == total else '[FAIL]'}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
