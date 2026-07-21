"""
scripts/check_resume.py

Acceptance harness for specs/0034 - compaction window safety (a resumed session must never overflow the
model's context window). Dep-free: no model, no network (stub summarizers). Proves both bugs are closed and
the byte-identical-when-normal invariant:

  * bounded_summary never hands a single summarize call more than the input budget on a huge history, and its
    fast path issues exactly one call for an input that already fits.
  * the ContextManager compacts an over-window working set UNDER the hard model-window ceiling - both when
    summarization shrinks it and when only the last-resort trim can (a big-summary stub).
  * sanitize_tail drops a dangling trailing tool_call / leading orphan result, and is a no-op on a clean tail.
  * a normal-size session is unchanged by the hard-cap pass (byte-identity).

Run:  python scripts/check_resume.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import config  # noqa: E402
from src.context import (ContextManager, estimate_tokens, bounded_summary, sanitize_tail)  # noqa: E402

_results = []


def check(label, cond):
    _results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


class _NullTraj:
    def log_turn(self, m):
        pass

    def log_compaction(self, *a):
        pass


class _FakeModel:
    """summarize returns a short briefing -> _compact shrinks."""
    def summarize(self, messages):
        return f"SUMMARY of {len(messages)} msgs"


class _BigSummaryModel:
    """summarize returns something huge -> even after _capped, compaction can't shrink a big kept tail, so the
    hard-cap loop must fall back to trimming."""
    def summarize(self, messages):
        return "X" * 600000


def _render(msgs):
    return "\n\n".join(f"[{m['role']}] {m['content']}" for m in msgs)


def _cm(model, working):
    return ContextManager("SYS PROMPT", model, _NullTraj(), initial_working=working)


def main():
    _saved = {k: getattr(config, k) for k in ("MODEL_MAX_TOKENS", "COMPACT_HARD_AT_TOKENS",
                                              "SUMMARIZE_INPUT_MAX_TOKENS", "COMPACT_AT_TOKENS", "COMPACT_KEEP_RECENT")}

    # =====================================================================================================
    # 1. bounded_summary never exceeds the input budget (the ROOT crash: summarize sent the whole history)
    # =====================================================================================================
    calls = []

    def once(text):
        calls.append(len(text))
        return "s" * 50

    budget = config.SUMMARIZE_INPUT_MAX_TOKENS * 4   # chars
    huge = [{"role": "user", "content": "x" * 40000} for _ in range(30)]   # ~1.2M chars ~= 300k tok
    out = bounded_summary(huge, budget, once, _render)
    check("bounded_summary: NO single call exceeds the input budget on a huge history",
          calls and all(c <= budget for c in calls))
    check("bounded_summary: chunked into more than one call for an over-budget input", len(calls) > 1)
    check("bounded_summary: returns a non-empty string", isinstance(out, str) and out)
    calls.clear()
    bounded_summary([{"role": "user", "content": "hi"}], budget, once, _render)
    check("bounded_summary: FAST PATH issues exactly ONE call for an input that already fits", len(calls) == 1)

    # =====================================================================================================
    # 2. sanitize_tail
    # =====================================================================================================
    dangling = [{"role": "user", "content": "q"}, {"role": "assistant", "content": "", "tool_calls": [{"id": "1"}]}]
    check("sanitize_tail: a TRAILING dangling assistant tool_call is dropped",
          sanitize_tail(dangling) == [{"role": "user", "content": "q"}])
    clean = [{"role": "user", "content": "q"},
             {"role": "assistant", "content": "", "tool_calls": [{"id": "1"}]},
             {"role": "tool", "tool_call_id": "1", "content": "r"}]
    check("sanitize_tail: a properly PAIRED tail is untouched (no-op)", sanitize_tail(clean) == clean)
    leading = [{"role": "tool", "tool_call_id": "1", "content": "orphan"}, {"role": "user", "content": "q"}]
    check("sanitize_tail: a LEADING orphan tool result is dropped",
          sanitize_tail(leading) == [{"role": "user", "content": "q"}])
    check("sanitize_tail: a plain assistant answer (no tool_calls) is kept",
          sanitize_tail([{"role": "assistant", "content": "done"}]) == [{"role": "assistant", "content": "done"}])

    # =====================================================================================================
    # 3. the hard model-window ceiling: an over-window resume compacts to fit
    # =====================================================================================================
    config.COMPACT_HARD_AT_TOKENS = 20000   # small ceiling for a fast, deterministic test
    over = [{"role": "user", "content": "x" * 40000} for _ in range(20)]
    cm = _cm(_FakeModel(), over)
    check("resume: the rehydrated working set is OVER the hard cap before context()",
          estimate_tokens(cm._base() + cm.working) > config.COMPACT_HARD_AT_TOKENS)
    ctx = cm.context()
    check("resume: context() compacts the SENT context UNDER the hard model-window ceiling",
          estimate_tokens(ctx) <= config.COMPACT_HARD_AT_TOKENS)
    ctx2 = _cm(_BigSummaryModel(), over).context()
    check("resume: when summarization can't shrink it, the trim fallback still fits it under the cap",
          estimate_tokens(ctx2) <= config.COMPACT_HARD_AT_TOKENS)

    # =====================================================================================================
    # 4. byte-identity: a normal-size session is untouched by the hard-cap pass
    # =====================================================================================================
    config.COMPACT_HARD_AT_TOKENS = _saved["COMPACT_HARD_AT_TOKENS"]   # restore the real (~120k) ceiling
    normal = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello there"}]
    cmn = _cm(_FakeModel(), normal)
    before = list(cmn.working)
    cmn.context()
    check("byte-identical: a normal-size session's working is unchanged by the hard-cap pass",
          cmn.working == before)

    check("config: CODE_MODEL_MAX_TOKENS + derived budgets exist and are ordered sensibly",
          config.MODEL_MAX_TOKENS >= config.COMPACT_HARD_AT_TOKENS > config.SUMMARIZE_INPUT_MAX_TOKENS > 0)

    for k, v in _saved.items():
        setattr(config, k, v)

    passed, total = sum(_results), len(_results)
    print(f"\nVERDICT: {passed}/{total} {'[OK]' if passed == total else '[FAIL]'}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
