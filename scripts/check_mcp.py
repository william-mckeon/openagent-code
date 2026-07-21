"""
scripts/check_mcp.py

Acceptance harness for specs/0029 - MCP web fence (route a web-marked MCP server through the same
untrusted-content fence + grounding read-ledger as the native web tools). Dep-free: no `mcp` SDK, no network,
no event loop (a fake result stands in for a real MCP call; _result_to_toolresult is a pure function). Proves:

  * _result_to_toolresult: a web-marked OK result is fenced + its URLs recorded (surfaced); a non-web result
    is byte-identical (no fence, no ledger); an error is never fenced; truncate-before-wrap keeps the fence.
  * _wrap tags a web tool-dict `web: True` (name = mcp__server__tool); a non-web dict has no `web` key.
  * tools.record_external extracts body + args URLs; a bare ctx is a safe no-op.
  * config.web_grounding_active() is True when MCP_WEB_ACTIVE even with ENABLE_WEB off; grounding's web check
    then runs (a cited MCP URL is grounded/checked), and is skipped when no web is active (byte-identical).

Run:  python scripts/check_mcp.py
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import config, grounding, mcp_client  # noqa: E402
from src import tools as tools_mod  # noqa: E402
from src.tools import _WEB_UNTRUSTED_OPEN, _WEB_UNTRUSTED_CLOSE, record_external  # noqa: E402
from src.permissions import Permissions  # noqa: E402

_results = []


def check(label, cond):
    _results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


class _FakeContent:
    def __init__(self, text):
        self.text = text


class _FakeResult:
    def __init__(self, text, is_error=False):
        self.content, self.isError = [_FakeContent(text)], is_error


class _FakeTool:
    def __init__(self, name):
        self.name, self.description, self.inputSchema = name, "desc", {"type": "object", "properties": {}}


class _Bare:
    pass


class _GCtx:
    depth, cwd, mutations, fetched, _verified_ok, spawn = 0, "", {}, {}, False, None


def _ctx(ws):
    return tools_mod.Context(ws, Permissions("default", {}, []))


def main():
    ws = os.path.realpath(tempfile.mkdtemp(prefix="mcp-ws-"))
    _saved = {k: getattr(config, k) for k in ("ENABLE_WEB", "MCP_WEB_ACTIVE", "VERIFY_GROUNDING_SEMANTIC")}

    # =====================================================================================================
    # 1. _result_to_toolresult: the choke point
    # =====================================================================================================
    ctx = _ctx(ws)
    rweb = mcp_client._result_to_toolresult(_FakeResult("Body cites https://ex.com/a for facts."), True, ctx,
                                            {"url": "https://q.com/x"})
    check("web result is fenced as untrusted external content",
          rweb.ok and _WEB_UNTRUSTED_OPEN in rweb.content and _WEB_UNTRUSTED_CLOSE in rweb.content)
    check("web result records BODY + ARGS URLs on the ledger (surfaced tier)",
          ctx.fetched.get("https://ex.com/a", {}).get("tier") == "surfaced"
          and ctx.fetched.get("https://q.com/x", {}).get("tier") == "surfaced")

    ctx2 = _ctx(ws)
    rplain = mcp_client._result_to_toolresult(_FakeResult("plain body https://ex.com/a"), False, ctx2, {})
    check("non-web result is NOT fenced and records nothing (byte-identical)",
          _WEB_UNTRUSTED_OPEN not in rplain.content and ctx2.fetched == {})

    rerr = mcp_client._result_to_toolresult(_FakeResult("boom", is_error=True), True, _ctx(ws), {})
    check("an ERROR result is never fenced, even for a web server, and returns ok=False",
          rerr.ok is False and _WEB_UNTRUSTED_OPEN not in rerr.content)

    big = "x" * 9000 + " https://ex.com/big"
    rbig = mcp_client._result_to_toolresult(_FakeResult(big), True, _ctx(ws), {})
    check("truncate-before-wrap keeps the closing fence intact on an oversized body",
          rbig.content.rstrip().endswith(_WEB_UNTRUSTED_CLOSE))

    # =====================================================================================================
    # 2. _wrap tags a web tool-dict
    # =====================================================================================================
    dw = mcp_client._wrap("tavily", None, _FakeTool("tavily_search"), is_web=True)
    check("_wrap: a web tool-dict is named mcp__<server>__<tool> and carries web:True",
          dw["name"] == "mcp__tavily__tavily_search" and dw.get("web") is True)
    dn = mcp_client._wrap("git", None, _FakeTool("status"), is_web=False)
    check("_wrap: a non-web tool-dict has NO web key (byte-identical)", "web" not in dn)

    # =====================================================================================================
    # 3. tools.record_external
    # =====================================================================================================
    rc = _ctx(ws)
    record_external(rc, "see https://a.com/p and https://b.com/q", {"urls": ["https://c.com/r"]})
    check("record_external extracts body + args URLs (surfaced)",
          all(rc.fetched.get(u, {}).get("tier") == "surfaced"
              for u in ("https://a.com/p", "https://b.com/q", "https://c.com/r")))
    record_external(_Bare(), "text", None)
    check("record_external on a bare ctx (no .fetched) is a safe no-op", True)

    # =====================================================================================================
    # 4. config.web_grounding_active() + the grounding gate
    # =====================================================================================================
    config.ENABLE_WEB = False
    config.MCP_WEB_ACTIVE = False
    check("web_grounding_active() is False when both native web and MCP web are off",
          config.web_grounding_active() is False)
    config.MCP_WEB_ACTIVE = True
    check("web_grounding_active() is True when a web MCP is active (even with CODE_ENABLE_WEB off)",
          config.web_grounding_active() is True)

    config.VERIFY_GROUNDING_SEMANTIC = False   # isolate the deterministic web-citation check
    check("problems() runs the web-citation check when an MCP web server is active",
          any("never" in p for p in grounding.problems("See https://made-up.example/x for the reference.", _GCtx())))
    config.MCP_WEB_ACTIVE = False
    check("problems() skips the web check when no web is active (byte-identical)",
          grounding.problems("See https://made-up.example/x for the reference.", _GCtx()) == [])

    for k, v in _saved.items():
        setattr(config, k, v)

    passed, total = sum(_results), len(_results)
    print(f"\nVERDICT: {passed}/{total} {'[OK]' if passed == total else '[FAIL]'}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
