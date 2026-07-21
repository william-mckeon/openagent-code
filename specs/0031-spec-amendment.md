# 0031 - spec amendment (a declined spec, once revised, re-proposes in place instead of dangling)

## Goal
The log review caught spec-first (specs/0025) doing its job right up to the last step: it proposed a spec,
the user DECLINED and said what was missing ("it should copy the folder over first"), and the agent then
ABANDONED the spec and acted ad hoc instead of folding the amendment in and re-proposing. The fix is small
and has no new flag - it rides `CODE_SPEC_FIRST` (a spec-less run is unaffected).

## Concepts
- **Re-propose amends in place.** `write_spec(action='propose')` built a FRESH spec dict with no `number`, so
  `specstore.save` minted the NEXT number (0002) - a re-propose spawned a second spec instead of revising the
  first. Now, when a DECLINED draft is still on `ctx.spec` (not approved), a re-propose carries its `number`
  (and `slug` - number is identity, slug is derived once, per 0025's non-goal) so `save` rewrites the SAME
  file. A brand-new spec (no prior draft this task, or the prior was already APPROVED/built) mints a fresh
  number exactly as before.
- **The decline steer is a directive, not a hint.** The decline tool-message now tells the agent to FOLD the
  user's feedback into the spec and call `write_spec(action='propose')` again (which amends the same spec),
  or ask what to change - never to proceed without an approved spec.
- **The prompt teaches the loop.** The SPEC-FIRST note gains a line: on a decline/change request, fold the
  feedback in and re-propose (it amends the same spec); don't abandon it and act ad hoc.

## Acceptance
- `src/tools.py`: `write_spec` propose carries a declined `ctx.spec`'s `number`+`slug` so a re-propose amends
  in place; the decline branch's message mandates fold-and-re-propose.
- `src/prompts.py`: the SPEC-FIRST note covers the decline -> revise -> re-propose loop.
- `scripts/check_specs.py`: a declined re-propose amends (same number, one file); an approved-then-new propose
  mints a fresh number.
- **Rides `CODE_SPEC_FIRST`**: a spec-less / flag-off run is byte-identical (write_spec isn't offered).

## Traps (each is a test)
- **Amend only a DECLINED draft.** The number is carried only when the prior `ctx.spec` is unapproved; an
  APPROVED (built) spec followed by a new propose is a genuinely NEW spec and mints a fresh number - don't
  clobber a shipped spec.
- **Number is identity, slug derived once.** Reuse BOTH so a re-title doesn't orphan the old file at a new
  slug; `save` reuses `spec['slug']` when present.
- **One file after an amendment.** A declined-then-revised spec leaves exactly one file in `.openagent/specs/`.

## Non-goals (v1)
- A distinct `action='revise'` (a re-`propose` on a declined draft already amends; no new verb needed).
- Version history of a spec's revisions (the file is rewritten in place; git history covers the maintainers'
  own specs, and the agent's `.openagent/` specs are working artifacts).
- Amending an APPROVED spec's title/scope (0025's non-goal stands: number = identity, slug = derived once).
