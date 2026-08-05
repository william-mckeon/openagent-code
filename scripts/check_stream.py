r"""
scripts/check_stream.py

Acceptance harness for specs/0043 — CODE_STREAM (stream the primary model turn). DEP-FREE: stdlib + src,
NEVER litellm. model.py imports litellm at module top, so we inject a FAKE litellm into sys.modules BEFORE
`from src import model` — the reassembler (_assemble_stream) and the _invoke seam are then exercised with a
fake chunk iterator and a recording fake litellm.completion, no network and no real litellm. Run:

    python scripts/check_stream.py
"""
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# --- fake litellm BEFORE importing src.model (dep-free) ------------------------------------------------
_fake = types.ModuleType("litellm")
_fake.suppress_debug_info = True
_fake.modify_params = True
_fake.completion = lambda **k: None
sys.modules["litellm"] = _fake

from types import SimpleNamespace          # noqa: E402
from src import config, model, trajectory  # noqa: E402

_results = []


def check(label, cond):
    _results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


# --- fake OpenAI-style streaming chunk builders --------------------------------------------------------
def _delta(content=None, reasoning=None, tool_calls=None):
    return SimpleNamespace(content=content, reasoning_content=reasoning, tool_calls=tool_calls)


def _tcd(index=0, id=None, name=None, args=None):
    fn = SimpleNamespace(name=name, arguments=args) if (name is not None or args is not None) else None
    return SimpleNamespace(index=index, id=id, function=fn)


def _chunk(delta=None, finish=None, usage=None, has_choice=True):
    choices = [SimpleNamespace(delta=delta, finish_reason=finish)] if has_choice else []
    return SimpleNamespace(choices=choices, usage=usage)


def _full_stream():
    """content split across chunks + a tool_call whose name/arguments arrive as index fragments +
    reasoning_content fragments + a terminal usage-only chunk (no choices)."""
    return [
        _chunk(_delta(reasoning="Let me ")),
        _chunk(_delta(reasoning="think.")),
        _chunk(_delta(content="Hello ")),
        _chunk(_delta(content="world")),
        _chunk(_delta(tool_calls=[_tcd(index=0, id="call_1", name="get_weather")])),
        _chunk(_delta(tool_calls=[_tcd(index=0, args='{"ci')])),
        _chunk(_delta(tool_calls=[_tcd(index=0, args='ty":"Tokyo"}')])),
        _chunk(_delta(), finish="tool_calls"),
        _chunk(usage=SimpleNamespace(prompt_tokens=25, completion_tokens=42), has_choice=False),
    ]


def main():
    saved_stream = config.STREAM

    # ---- reassembly equivalence --------------------------------------------------------------------
    resp = model._assemble_stream(iter(_full_stream()))
    msg = resp.choices[0].message
    check("reassembly: content concatenated", msg.content == "Hello world")
    check("reassembly: reasoning_content concatenated", msg.reasoning_content == "Let me think.")
    check("reassembly: tool_call id preserved", msg.tool_calls[0].id == "call_1")
    check("reassembly: tool_call name preserved", msg.tool_calls[0].function.name == "get_weather")
    check("reassembly: tool_call arguments fragments joined",
          msg.tool_calls[0].function.arguments == '{"city":"Tokyo"}')
    check("reassembly: usage from terminal chunk", resp.usage.prompt_tokens == 25 and resp.usage.completion_tokens == 42)
    check("reassembly: finish_reason captured", resp.choices[0].finish_reason == "tool_calls")

    # ---- specs/0064: show_reasoning tees the thinking to stdout (display-only) ----------------------
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        r_on = model._assemble_stream(iter(_full_stream()), show_reasoning=True)
    printed = buf.getvalue()
    check("show_reasoning ON: the reasoning is teed to stdout (with a 'thinking' marker)",
          "Let me think." in printed and "thinking" in printed)
    check("show_reasoning ON: the reassembled response is UNCHANGED (display-only)",
          r_on.choices[0].message.content == "Hello world"
          and r_on.choices[0].message.reasoning_content == "Let me think.")
    buf2 = io.StringIO()
    with contextlib.redirect_stdout(buf2):
        model._assemble_stream(iter(_full_stream()))   # default show_reasoning=False
    check("show_reasoning OFF (default): nothing printed (byte-identical)", buf2.getvalue() == "")
    check("Model plumbs show_reasoning (default False)",
          model.Model(None, None, show_reasoning=True).show_reasoning is True
          and model.Model(None, None).show_reasoning is False)
    _sr = os.environ.pop("CODE_SHOW_REASONING", None)
    sr_off = config._as_bool(os.environ.get("CODE_SHOW_REASONING", "false")) is False
    if _sr is not None:
        os.environ["CODE_SHOW_REASONING"] = _sr
    check("CODE_SHOW_REASONING defaults False when unset (opt-in)", sr_off)

    # ---- dropped-call detection (empty stream) -----------------------------------------------------
    empty = model._assemble_stream(iter([_chunk(_delta()),
                                         _chunk(usage=SimpleNamespace(prompt_tokens=1, completion_tokens=0),
                                                has_choice=False)]))
    em = empty.choices[0].message
    dropped = (not (em.content or "").strip()) and (not (em.tool_calls or []))   # complete()'s exact test
    check("dropped-call: empty stream -> content None + no tool_calls", em.content is None and em.tool_calls is None)
    check("dropped-call: computes True the same way complete() does", dropped is True)

    # ---- flag-OFF byte-identity: _invoke passes kwargs verbatim, adds NO stream keys ---------------
    m = model.Model(None, None)
    config.STREAM = False
    seen = {}
    model.litellm.completion = lambda **k: (seen.update(k) or "RESP_SENTINEL")
    kwargs = {"model": "x", "messages": [], "temperature": 0}
    out = m._invoke(kwargs)
    check("flag OFF: _invoke returns litellm.completion(**kwargs) verbatim", out == "RESP_SENTINEL")
    check("flag OFF: NO stream / stream_options keys added (byte-identical request)",
          "stream" not in seen and "stream_options" not in seen)
    check("flag OFF: caller kwargs dict not mutated", "stream" not in kwargs)

    # ---- flag-ON: _invoke streams + reassembles, without mutating the caller dict ------------------
    config.STREAM = True
    captured = {}
    model.litellm.completion = lambda **k: (captured.update(k) or iter(_full_stream()))
    kwargs2 = {"model": "x", "messages": [], "temperature": 0}
    resp2 = m._invoke(kwargs2)
    check("flag ON: stream=True + include_usage sent", captured.get("stream") is True
          and captured.get("stream_options") == {"include_usage": True})
    check("flag ON: returns a reassembled response", resp2.choices[0].message.content == "Hello world")
    check("flag ON: caller kwargs dict still not mutated", "stream" not in kwargs2)

    # ---- summarize NEVER streams, even with STREAM=True --------------------------------------------
    config.STREAM = True
    seen2 = {}
    model.litellm.completion = lambda **k: (seen2.update(k) or
                                            SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="s"))]))
    out2 = model.Model(None, None)._summarize_once("some rendered text")
    check("summarize: returns content", out2 == "s")
    check("summarize: does NOT stream even when CODE_STREAM=True", "stream" not in seen2)

    config.STREAM = saved_stream

    # ---- provenance invariants ---------------------------------------------------------------------
    check("SCHEMA_VERSION unchanged (0.13.0)", trajectory.Trajectory.SCHEMA_VERSION == "0.13.0")
    fp = config.safety_fingerprint()
    check("CODE_STREAM absent from safety_fingerprint", "STREAM" not in fp and "stream" not in repr(fp).lower())

    # ---- default proven against the fallback, not the live .env ------------------------------------
    _env = os.environ.pop("CODE_STREAM", None)
    default_off = config._as_bool(os.environ.get("CODE_STREAM", "false")) is False
    if _env is not None:
        os.environ["CODE_STREAM"] = _env
    check("CODE_STREAM defaults False when unset (opt-in)", default_off)

    passed, total = sum(_results), len(_results)
    print(f"\nVERDICT: {passed}/{total} {'[OK]' if passed == total else '[FAIL]'}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
