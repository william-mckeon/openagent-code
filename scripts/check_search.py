"""
scripts/check_search.py

Acceptance harness for specs/0024 — web search. Dep-free and NETWORK-FREE: the pure parsers / render /
load_provider are exercised directly, and run() is driven through a stub provider registered in
search.BUILTINS, so NOTHING ever hits api.tavily.com or any endpoint (which would violate the opt-in-egress
ethos the phase is built on). Run:

    python scripts/check_search.py

Exits 0 only if every check holds — including that CODE_ENABLE_WEB off is byte-identical (no web tools).
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import config, search, grounding  # noqa: E402
from src.tools import _wrap_external, _record_fetch, _WEB_UNTRUSTED_OPEN, _WEB_UNTRUSTED_CLOSE  # noqa: E402
from src.toolset import active_tools  # noqa: E402

_results = []


def check(label, cond):
    _results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


class _Ctx:
    def __init__(self):
        self.fetched = {}


def main():
    _saved = {k: getattr(config, k) for k in ("ENABLE_WEB", "SEARCH_PROVIDER", "SEARCH_KEY",
                                              "SEARCH_URL", "SEARCH_MAX_RESULTS")}
    config.SEARCH_MAX_RESULTS = 5

    # -- pure parsers: provider JSON -> the uniform {title, url, snippet} + answer ------------------------
    tav = search.parse_tavily({"answer": "print() writes to stdout.",
                               "results": [{"title": "print - Python docs", "url": "https://docs.python.org/3/library/functions.html#print",
                                            "content": "Print objects to the text stream file."},
                                           {"title": "t2", "url": "u2", "content": "c2"}]})
    check("parse_tavily -> uniform results (title/url/snippet) + preserves the synthesized answer",
          tav["answer"].startswith("print()") and len(tav["results"]) == 2
          and tav["results"][0]["snippet"].startswith("Print objects")
          and set(tav["results"][0]) == {"title", "url", "snippet"})

    gen = search.parse_generic({"results": [{"title": "g", "link": "http://g/1", "description": "gd"}]})
    check("parse_generic maps a BYO shape (link/description) to the uniform result",
          gen["results"] == [{"title": "g", "url": "http://g/1", "snippet": "gd"}])
    check("parse_generic falls back to raw text as one snippet on an unparseable body",
          search.parse_generic(None, "raw body text")["results"][0]["snippet"] == "raw body text")

    # -- load_provider: builtins, unknown (fail-safe), dotted module:func --------------------------------
    for name in ("tavily", "generic", "searxng", "brave"):
        check(f"load_provider('{name}') resolves the builtin", callable(search.load_provider(name)))
    unk = search.load_provider("bogus-xyz")
    check("load_provider(unknown) FAILS SAFE - returns a provider that yields an error payload, never raises",
          callable(unk) and unk("q", 5).get("error") and "unknown" in unk("q", 5)["error"].lower())
    dotted = search.load_provider("no.such.module:func")
    check("load_provider(bad dotted module:func) fails safe too", dotted("q", 5).get("error"))

    # -- run(): clamps, never raises, coerces a bad adapter return ---------------------------------------
    search.BUILTINS["_stub"] = lambda q, n: search._payload(
        results=[{"title": f"r{i}", "url": f"u{i}", "snippet": "s"} for i in range(10)], answer="A")
    search.BUILTINS["_boom"] = lambda q, n: (_ for _ in ()).throw(RuntimeError("provider blew up"))
    search.BUILTINS["_none"] = lambda q, n: None
    # audit #7: a custom provider returning a NON-LIST 'results' must not make run() raise (the clamp runs
    # outside the provider try/except, so it has to coerce).
    search.BUILTINS["_badlist"] = lambda q, n: {"results": 5, "answer": "", "error": ""}
    try:
        config.SEARCH_PROVIDER = "_stub"
        p = search.run("how to use print in python", max_results=3)
        check("run() hard-clamps results to max_results", len(p["results"]) == 3 and p["answer"] == "A")
        config.SEARCH_PROVIDER = "_boom"
        pb = search.run("q")
        check("run() NEVER raises when the adapter throws - returns an error payload",
              pb.get("error") and "blew up" in pb["error"])
        config.SEARCH_PROVIDER = "_none"
        check("run() coerces a non-dict/None adapter return into an error payload",
              search.run("q").get("error"))
        check("run() on an empty query -> error payload (no provider call)",
              search.run("   ").get("error"))
        config.SEARCH_PROVIDER = "_badlist"
        check("run() coerces a NON-LIST 'results' to [] and does not raise (audit #7)",
              search.run("q")["results"] == [])
    finally:
        for k in ("_stub", "_boom", "_none", "_badlist"):
            search.BUILTINS.pop(k, None)

    # audit #8: a dotted module:Func provider preserves case - the whole choice must NOT be lowercased, or
    # an uppercase function/module name fails to import.
    import types as _types, sys as _sys
    _fake = _types.ModuleType("faketestprovider")
    _fake.Func = lambda q, n: search._payload(answer="from a custom provider")
    _sys.modules["faketestprovider"] = _fake
    try:
        prov = search.load_provider("faketestprovider:Func")
        check("load_provider preserves case in a dotted module:Func (uppercase resolves)",
              prov("q", 5).get("answer") == "from a custom provider")
    finally:
        _sys.modules.pop("faketestprovider", None)

    # -- render(): numbered list; an error renders as its message ----------------------------------------
    r = search.render({"answer": "A", "results": [{"title": "T", "url": "U", "snippet": "S"}], "error": ""})
    check("render() emits a numbered list with the answer line", "Answer: A" in r and "1. T" in r and "U" in r)
    check("render() of an error payload is just the message",
          search.render({"error": "web_search (tavily) is not configured.", "results": [], "answer": ""})
          == "web_search (tavily) is not configured.")

    # -- the not-configured guards fire BEFORE any HTTP call (no network) ---------------------------------
    config.SEARCH_KEY, config.SEARCH_URL = "", ""
    check("tavily with no SEARCH_KEY -> not-configured (never calls the network)",
          "not configured" in search._tavily("q", 5)["error"])
    check("generic with no SEARCH_URL -> not-configured", "not configured" in search._generic("q", 5)["error"])

    # -- untrusted-content boundary (tools helpers) ------------------------------------------------------
    wrapped = _wrap_external("hello from the web")
    check("web content is wrapped in the untrusted boundary",
          _WEB_UNTRUSTED_OPEN in wrapped and _WEB_UNTRUSTED_CLOSE in wrapped and "hello from the web" in wrapped)
    c = _Ctx()
    _record_fetch(c, "https://x.com/p", "page text")
    check("_record_fetch records the fetched page on ctx.fetched (the grounding ledger)",
          c.fetched == {"https://x.com/p": "page text"})

    class _Bare:  # a ctx without .fetched (older/test) must not crash the recorder
        pass
    _record_fetch(_Bare(), "u", "t")
    check("_record_fetch on a ctx without .fetched is a safe no-op", True)

    # -- grounding: a cited+fetched URL is a source; a cited-but-unfetched URL is a phantom --------------
    config.ENABLE_WEB = True
    check("cited_urls extracts a bare http(s) URL from prose",
          grounding.cited_urls("see https://docs.python.org/3/x for print") == {"https://docs.python.org/3/x"})
    check("web_citation_problems flags a cited URL never fetched",
          len(grounding.web_citation_problems("per https://a.com/p it works", {})) == 1)
    check("a cited URL present in the fetched ledger is grounded (no problem)",
          grounding.web_citation_problems("per https://a.com/p it works", {"https://a.com/p": "text"}) == [])
    check("URL match is normalized (trailing slash / case)",
          grounding.web_citation_problems("see https://A.com/P/", {"https://a.com/p": "t"}) == [])

    # -- CODE_ENABLE_WEB gate: on -> web tools offered; off -> absent (byte-identical) --------------------
    config.ENABLE_WEB = True
    on = {t["name"] for t in active_tools()}
    config.ENABLE_WEB = False
    off = {t["name"] for t in active_tools()}
    check("CODE_ENABLE_WEB on -> web_fetch & web_search offered",
          {"web_fetch", "web_search"} <= on)
    check("CODE_ENABLE_WEB off -> both absent (flag-off byte-identical toolset)",
          not ({"web_fetch", "web_search"} & off))

    for k, v in _saved.items():
        setattr(config, k, v)

    passed, total = sum(_results), len(_results)
    print(f"\nVERDICT: {passed}/{total} {'[OK]' if passed == total else '[FAIL]'}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
