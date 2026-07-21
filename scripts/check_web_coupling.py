"""
scripts/check_web_coupling.py

Acceptance harness for specs/0028 - web-search grounding coupling (a surfaced URL grounds without a redundant
fetch). Dep-free: no model, no network (search.run is stubbed). Proves the two-tier read-ledger and the
byte-identical-when-web-off invariant:

  * _record_fetch = STRONG tier; _record_surfaced = WEAK tier; no-downgrade (fetch wins, surface never
    overwrites a fetched page); a bare/no-.fetched ctx is a safe no-op.
  * search.surfaced_sources extracts (url, snippet) pairs and skips empty URLs.
  * web_search records each result URL as a surfaced source (end-to-end, stubbed provider).
  * grounding: web_citation_problems grounds a surfaced-only URL; _cited_fetched unpacks {content,tier} and
    prefers the fetched page on collision; _verifier_task labels a snippet distinctly; legacy str tolerated.
  * Web OFF -> web_search doesn't record (disabled), byte-identical.

Run:  python scripts/check_web_coupling.py
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import config, grounding, search  # noqa: E402
from src import tools as tools_mod  # noqa: E402
from src.tools import _record_fetch, _record_surfaced  # noqa: E402
from src.permissions import Permissions  # noqa: E402

_results = []


def check(label, cond):
    _results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


class _Bare:  # a ctx without .fetched
    pass


def main():
    ws = os.path.realpath(tempfile.mkdtemp(prefix="webcoup-ws-"))
    _saved = {k: getattr(config, k) for k in ("ENABLE_WEB",)}

    # =====================================================================================================
    # 1. the two tiers + no-downgrade
    # =====================================================================================================
    c = tools_mod.Context(ws, Permissions("default", {}, []))
    _record_surfaced(c, "https://a.com/p", "a snippet")
    check("_record_surfaced records the WEAK tier",
          c.fetched["https://a.com/p"] == {"content": "a snippet", "tier": "surfaced"})
    _record_fetch(c, "https://a.com/p", "the full page")
    check("_record_fetch UPGRADES a surfaced URL to the STRONG (fetched) tier",
          c.fetched["https://a.com/p"] == {"content": "the full page", "tier": "fetched"})
    _record_surfaced(c, "https://a.com/p", "a later snippet")
    check("_record_surfaced does NOT downgrade a fetched full page to a snippet",
          c.fetched["https://a.com/p"]["tier"] == "fetched")
    _record_surfaced(_Bare(), "u", "s")
    _record_surfaced(c, "", "s")
    check("_record_surfaced on a bare ctx / empty url is a safe no-op", "" not in c.fetched)

    # =====================================================================================================
    # 2. search.surfaced_sources (pure)
    # =====================================================================================================
    payload = {"results": [{"title": "T", "url": "https://a.com/p", "snippet": "snip A"},
                           {"title": "U", "url": "", "snippet": "no url"},
                           {"title": "V", "url": "https://b.com/q", "snippet": "snip B"}],
               "answer": "A", "error": ""}
    srcs = dict(search.surfaced_sources(payload))
    check("surfaced_sources extracts (url, snippet) pairs and SKIPS empty URLs",
          srcs == {"https://a.com/p": "snip A", "https://b.com/q": "snip B"})

    # =====================================================================================================
    # 3. web_search records surfaced sources end-to-end (stubbed provider)
    # =====================================================================================================
    _orig_run = search.run
    search.run = lambda q, max_results=None: payload   # stub the provider - no network
    try:
        config.ENABLE_WEB = True
        cs = tools_mod.Context(ws, Permissions("default", {}, []))
        res = tools_mod.web_search({"query": "hello"}, cs)
        check("web_search records each result URL as a SURFACED source on ctx.fetched",
              res.ok and cs.fetched.get("https://a.com/p", {}).get("tier") == "surfaced"
              and cs.fetched.get("https://b.com/q", {}).get("tier") == "surfaced" and "" not in cs.fetched)
        # web off -> disabled, records nothing (byte-identical)
        config.ENABLE_WEB = False
        co = tools_mod.Context(ws, Permissions("default", {}, []))
        ro = tools_mod.web_search({"query": "hello"}, co)
        check("web OFF: web_search is disabled and records nothing (byte-identical)",
              ro.ok is False and co.fetched == {})
    finally:
        search.run = _orig_run

    # =====================================================================================================
    # 4. grounding is tier-aware
    # =====================================================================================================
    surfaced_led = {"https://a.com/p": {"content": "snip", "tier": "surfaced"}}
    check("web_citation_problems grounds a SURFACED-only URL (reads keys, either tier)",
          grounding.web_citation_problems("per https://a.com/p it works", surfaced_led) == [])
    check("web_citation_problems still flags a URL on NO tier of the ledger",
          len(grounding.web_citation_problems("per https://z.com/x it works", surfaced_led)) == 1)

    # prefer the fetched full page on a normalized-URL collision
    collide = {"https://a.com/p": {"content": "snippet only", "tier": "surfaced"},
               "https://a.com/p/": {"content": "the full page", "tier": "fetched"}}
    cf = grounding._cited_fetched("see https://a.com/p", collide)
    check("_cited_fetched prefers the FETCHED full page over a surfaced snippet on collision",
          cf["https://a.com/p"]["tier"] == "fetched" and cf["https://a.com/p"]["content"] == "the full page")

    # the verifier task labels a snippet distinctly and a full page as FETCHED
    tsnip = grounding._verifier_task("see https://a.com/p", set(),
                                     {"https://a.com/p": {"content": "snip", "tier": "surfaced"}})
    check("verifier task labels a surfaced source as a SEARCH SNIPPET (weak), not a fetched page",
          "SEARCH SNIPPETS" in tsnip and "FETCHED WEB SOURCES" not in tsnip)
    tfull = grounding._verifier_task("see https://a.com/p", set(),
                                     {"https://a.com/p": {"content": "page", "tier": "fetched"}})
    check("verifier task labels a fetched source as FETCHED WEB SOURCES",
          "FETCHED WEB SOURCES" in tfull and "SEARCH SNIPPETS" not in tfull)

    # legacy bare-str ledger value is tolerated (reads as fetched)
    check("_web_content / _web_tier tolerate a legacy bare-str value (reads as fetched)",
          grounding._web_content("legacy") == "legacy" and grounding._web_tier("legacy") == "fetched")
    tlegacy = grounding._verifier_task("see https://a.com/p", set(), {"https://a.com/p": "legacy text"})
    check("verifier task tolerates a legacy str value (renders under FETCHED)",
          "FETCHED WEB SOURCES" in tlegacy and "legacy text" in tlegacy)

    for k, v in _saved.items():
        setattr(config, k, v)

    passed, total = sum(_results), len(_results)
    print(f"\nVERDICT: {passed}/{total} {'[OK]' if passed == total else '[FAIL]'}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
