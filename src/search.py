"""
src/search.py

Web search providers (Phase 24 / specs/0024) — the pluggable, LLM-optimized upgrade of the bare web_search
stub. `CODE_SEARCH_PROVIDER` selects an ADAPTER that shapes the request, calls the endpoint, and PARSES the
response into ONE uniform shape `[{title, url, snippet}]` (+ an optional synthesized `answer`). Tavily is
the flagship (hosted, returns a direct answer + sources); `generic` is the original BYO-endpoint behavior
(the DEFAULT, so existing configs are unchanged); `searxng`/`brave` and a dotted `module:func` let an
operator drop in their own (e.g. a self-hosted SearXNG — the data-sovereign path), exactly like the
adaptive-effort policy is switchable.

Parsing / rendering / provider-loading are PURE functions (no network), so the acceptance harness exercises
them offline — the same 'pure core + thin transport' split effort.py uses. Every path is FAIL-SAFE: a
missing key, an unset URL, an HTTP error, or a bad custom provider returns a clear 'not configured'/error
payload and NEVER raises into the tool. Off by default via CODE_ENABLE_WEB (the toolset gate); this module
runs only when a web tool is actually called. Imports only config + logsetup + stdlib.
"""
import importlib

from . import config
from .logsetup import get_logger

log = get_logger("search")

_SNIPPET_CAP = 500   # per-result snippet bound — a search result is a pointer to a page, not the page


def _payload(results=None, answer="", error=""):
    """The one uniform shape every adapter returns and the tool + grounding consume."""
    return {"results": list(results or []), "answer": answer or "", "error": error or ""}


def _norm_result(title, url, snippet):
    """Coerce one provider's result fields into the uniform {title, url, snippet}, clipping the snippet."""
    return {"title": (title or "").strip(), "url": (url or "").strip(),
            "snippet": " ".join((snippet or "").split())[:_SNIPPET_CAP]}


# -- pure parsers (network-free; the harness calls these directly) ------------

def parse_tavily(data):
    """Tavily JSON {answer, results:[{title,url,content}]} -> the uniform payload."""
    results = [_norm_result(x.get("title"), x.get("url"), x.get("content"))
               for x in (data.get("results") or []) if isinstance(x, dict)]
    return _payload(results=results, answer=str(data.get("answer") or ""))


def parse_generic(data, raw_text=""):
    """Best-effort parse of a BYO endpoint's body into the uniform payload. Walks the common shapes (a list
    of results under results/items/data, an answer at the top); on an unparseable body, falls back to the
    raw text as one snippet, so a search never returns nothing usable."""
    rows, answer = [], ""
    if isinstance(data, dict):
        rows = data.get("results") or data.get("items") or data.get("data") or []
        answer = str(data.get("answer") or data.get("answerBox") or "")
    elif isinstance(data, list):
        rows = data
    results = [_norm_result(x.get("title") or x.get("name"),
                            x.get("url") or x.get("link") or x.get("href"),
                            x.get("snippet") or x.get("content") or x.get("description"))
               for x in rows if isinstance(x, dict)]
    if not results and not answer:
        return _payload(results=[_norm_result("result", "", raw_text[:_SNIPPET_CAP * 4])])
    return _payload(results=results, answer=answer)


def parse_searxng(data):
    """SearXNG `?format=json` {results:[{title,url,content}]} -> the uniform payload."""
    results = [_norm_result(x.get("title"), x.get("url"), x.get("content"))
               for x in (data.get("results") or []) if isinstance(x, dict)]
    return _payload(results=results, answer=str(data.get("answer") or ""))


def parse_brave(data):
    """Brave Search API {web:{results:[{title,url,description}]}} -> the uniform payload."""
    web = (data.get("web") or {}).get("results") or []
    results = [_norm_result(x.get("title"), x.get("url"), x.get("description"))
               for x in web if isinstance(x, dict)]
    return _payload(results=results)


# -- adapters (thin transport; httpx lazy-imported inside each) ---------------

def _tavily(query, max_results):
    if not config.SEARCH_KEY:
        return _payload(error="web_search (tavily) is not configured. Set CODE_SEARCH_KEY to your Tavily "
                              "API key (tvly-...), or switch CODE_SEARCH_PROVIDER.")
    import httpx
    r = httpx.post("https://api.tavily.com/search",
                   json={"api_key": config.SEARCH_KEY, "query": query, "max_results": max_results,
                         "include_answer": True},   # the flagship 'answer' only comes back when asked for
                   timeout=30)
    if r.status_code != 200:
        return _payload(error=f"tavily search HTTP {r.status_code}")
    return parse_tavily(r.json())


def _generic(query, max_results):
    if not config.SEARCH_URL:
        return _payload(error="web_search (generic) is not configured. Set CODE_SEARCH_URL to your search "
                              "endpoint, or switch CODE_SEARCH_PROVIDER (e.g. tavily).")
    import httpx
    headers = {"Content-Type": "application/json"}
    if config.SEARCH_KEY:
        headers["Authorization"] = f"Bearer {config.SEARCH_KEY}"
    r = httpx.post(config.SEARCH_URL, json={"query": query}, headers=headers, timeout=30)
    if r.status_code != 200:
        return _payload(error=f"search HTTP {r.status_code}")
    try:
        data = r.json()
    except ValueError:
        data = None
    return parse_generic(data, r.text)


def _searxng(query, max_results):
    if not config.SEARCH_URL:
        return _payload(error="web_search (searxng) is not configured. Set CODE_SEARCH_URL to your SearXNG "
                              "instance (.../search).")
    import httpx
    r = httpx.get(config.SEARCH_URL, params={"q": query, "format": "json"}, timeout=30)
    if r.status_code != 200:
        return _payload(error=f"searxng search HTTP {r.status_code}")
    return parse_searxng(r.json())


def _brave(query, max_results):
    if not config.SEARCH_KEY:
        return _payload(error="web_search (brave) is not configured. Set CODE_SEARCH_KEY to your Brave "
                              "Search API subscription token.")
    import httpx
    r = httpx.get("https://api.search.brave.com/res/v1/web/search",
                  params={"q": query, "count": max_results},
                  headers={"X-Subscription-Token": config.SEARCH_KEY, "Accept": "application/json"},
                  timeout=30)
    if r.status_code != 200:
        return _payload(error=f"brave search HTTP {r.status_code}")
    return parse_brave(r.json())


BUILTINS = {"tavily": _tavily, "generic": _generic, "searxng": _searxng, "brave": _brave}


def load_provider(name=None):
    """Resolve the configured provider to a callable(query, max_results) -> payload. A builtin name, or a
    dotted 'module:func' an operator wrote (their own SearXNG client, ...). Unknown / unimportable -> a
    fail-safe provider that reports 'not configured' and NEVER raises — the mirror of effort.load_policy
    falling back to the reactive default."""
    raw = (name or config.SEARCH_PROVIDER or "generic").strip()
    choice = raw.lower()                       # only the builtin-NAME test is case-insensitive
    if choice in BUILTINS:
        return BUILTINS[choice]
    if ":" in raw:                             # partition the ORIGINAL: a module path / function name is case-sensitive
        mod_name, _, fn = raw.partition(":")
        try:
            return getattr(importlib.import_module(mod_name.strip()), fn.strip())
        except Exception as e:  # noqa: BLE001 - a bad custom provider must not crash the run
            msg = str(e)   # bind now: `e` is unbound outside the except block, so the lambda can't close over it
            log.warning("search provider %r failed to load (%s) - reporting not-configured", raw, msg)
            return lambda q, n: _payload(error=f"search provider {raw!r} could not be loaded: {msg}")
    return lambda q, n: _payload(
        error=f"unknown CODE_SEARCH_PROVIDER {raw!r} - use tavily | generic | searxng | brave | module:func.")


def run(query, max_results=None):
    """Run a web search through the configured provider. Returns a payload {results, answer, error}; NEVER
    raises (a network / parse / provider failure becomes a friendly `error`). Results are hard-clamped to
    max_results (default CODE_SEARCH_MAX_RESULTS)."""
    query = (query or "").strip()
    if not query:
        return _payload(error="web_search needs a non-empty query.")
    n = max_results or config.SEARCH_MAX_RESULTS
    provider = load_provider()
    try:
        payload = provider(query, n)
        if not isinstance(payload, dict):
            payload = _payload(error="the search provider returned an unexpected result.")
    except Exception as e:  # noqa: BLE001 - a provider (esp. a custom one) must never raise into the tool
        log.warning("web_search failed (%s)", e)
        return _payload(error=f"search error: {type(e).__name__}: {e}")
    _r = payload.get("results")   # coerce before slicing: a custom provider returning a non-list must not raise here
    payload["results"] = (_r if isinstance(_r, list) else [])[:n]   # hard clamp regardless of what the adapter did
    payload.setdefault("answer", "")
    payload.setdefault("error", "")
    return payload


def surfaced_sources(payload):
    """The (url, snippet) pairs a search payload surfaced - what web_search records on the read-ledger as WEAK
    (snippet-only) sources (specs/0028), so a cited result URL grounds without a redundant web_fetch. PURE
    (network-free, exercised offline); skips empty URLs. Snippets are already clamped to _SNIPPET_CAP by
    _norm_result, so the recorded content is pre-bounded."""
    out = []
    for r in (payload.get("results") or []):
        if not isinstance(r, dict):
            continue
        url = (r.get("url") or "").strip()
        if url:
            out.append((url, r.get("snippet") or ""))
    return out


def render(payload):
    """A compact, NUMBERED markdown rendering of a search payload for the model (replaces the raw-text
    dump). An error payload renders as its message."""
    if payload.get("error"):
        return payload["error"]
    lines = []
    if payload.get("answer"):
        lines.append(f"Answer: {payload['answer']}\n")
    results = payload.get("results") or []
    if not results:
        lines.append("(no results)")
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r.get('title') or '(untitled)'}\n   {r.get('url')}\n   {r.get('snippet')}")
    return "\n".join(lines).strip()
