# 0028 - web-search grounding coupling (a surfaced URL grounds without a redundant fetch)

## Goal
Close the friction the log review surfaced around web search: `web_search` wraps its results in the
untrusted fence but does NOT record them on the `ctx.fetched` read-ledger, so a URL the model cites straight
from the results is flagged a PHANTOM web citation by the grounding gate unless it *also* `web_fetch`es it -
a redundant round-trip the tool descriptions had to warn around. Give the ledger two tiers so a surfaced
result URL counts as (weak) grounded evidence, while a fetched full page stays the strong tier. Rides the
existing `CODE_ENABLE_WEB` (no new flag); web off (the default) is byte-identical (the ledger stays empty).

## Concepts
- **Two-tier read-ledger.** `ctx.fetched` values become `{content, tier}` with `tier` in `fetched` (full
  page, STRONG) or `surfaced` (search snippet, WEAK). `_record_fetch` tags `fetched` and ALWAYS writes, so a
  fetch UPGRADES a prior surface. New `_record_surfaced` tags `surfaced` and writes ONLY when the URL is
  absent or already surfaced - it NEVER downgrades a fetched full page to a snippet.
- **web_search records surfaced sources.** After `search.run`, each result URL is recorded via
  `_record_surfaced` (through the pure `search.surfaced_sources(payload)` helper). So a cited result URL
  passes the deterministic phantom-citation check with no `web_fetch`.
- **Grounding is tier-aware.** `web_citation_problems` is unchanged (it reads KEYS, so either tier grounds a
  URL). `_cited_fetched` unpacks `{content, tier}`, prefers the FETCHED entry on a normalized-URL collision,
  and returns each source's tier. `_verifier_task` renders FETCHED full pages and SEARCH SNIPPETS under
  DISTINCT untrusted-data labels, so the Tier-2 verifier doesn't treat a snippet as full-page support. All of
  it tolerates a legacy bare-str value (reads as `fetched`).
- **The prompt.** The web note tells the model a surfaced URL is a weak cited source it may cite WITHOUT
  re-fetching, and to `web_fetch` only for the full page / a precise claim - so it stops re-fetching every
  search hit (which defeated the coupling).

## Acceptance
- `src/tools.py`: `Context.fetched` two-tier; `_record_fetch` = strong (always wins); `_record_surfaced` =
  weak (no downgrade); `web_search` records each surfaced result URL; tool descriptions teach the tiers.
- `src/search.py`: pure `surfaced_sources(payload) -> [(url, snippet)]` (skips empty URLs).
- `src/grounding.py`: `_web_content` / `_web_tier`; `_cited_fetched` returns `{url: {content, tier}}` and
  prefers strong on collision; `_verifier_task` labels snippets distinctly; `web_citation_problems` docstring.
- `src/prompts.py`: the web note teaches surfaced-URL-is-weak-cite.
- `scripts/check_web_coupling.py` - dep-free, no network. `check_search.py` / `check_grounding.py` fixtures
  updated to the tiered shape.
- **Web OFF is byte-identical**: with `CODE_ENABLE_WEB=false` the web tools aren't offered, `ctx.fetched`
  stays `{}`, no tier ever materializes, and grounding's web checks are skipped.

## Traps (each is a test)
- **No downgrade.** Fetch-then-surface keeps the strong entry; surface-then-fetch upgrades to strong.
- **Prefer strong on collision.** When a fetched and a surfaced raw key normalize to the same URL,
  `_cited_fetched` keeps the full page.
- **Snippet is weak.** The verifier is told a SEARCH SNIPPET is not full-page support; a claim needing the
  page is not grounded by a snippet alone.
- **Legacy tolerance.** A bare-str ledger value (older data / a direct caller) reads as `fetched` and never
  crashes `_cited_fetched` / `_verifier_task`.
- **Keys, not values.** `web_citation_problems` grounds a URL by its presence as a KEY, so a surfaced-only URL
  is grounded exactly like a fetched one.

## Non-goals (v1)
- A deterministic "backed only by a snippet - fetch for depth" nudge in the answer (relies on the Tier-2
  verifier's snippet label instead; a deterministic note risks re-introducing the exact noise this removes).
- Recording the synthesized `answer`'s implied sources (only explicit result URLs are surfaced).
- Per-result url->snippet attribution beyond the bounded snippet already clamped by `_SNIPPET_CAP`.
