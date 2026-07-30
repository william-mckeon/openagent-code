# 0043 — streaming

Status: implemented
Flag: `CODE_STREAM` (default off)

## Goal

Stream the primary model turn so a long-reasoning model (thinkingmachines/Inkling on Together) can emit its
answer incrementally instead of the caller waiting for the whole completion. Non-streaming already works;
this is an opt-in for responsiveness on long generations, not a fix for a broken path. The hard requirement
is that turning it on changes HOW the response arrives, never WHAT the rest of the agent sees — the
dropped-call retry, trajectory logging, and the planner reasoning fold must all consume a streamed turn
identically to a non-streamed one.

## Concepts

- **One call site, one seam.** `complete()` made a single `litellm.completion(**kwargs)` (model.py:146). That
  becomes `self._invoke(kwargs)`. With `CODE_STREAM` off, `_invoke` returns `litellm.completion(**kwargs)`
  with the untouched kwargs — byte-identical request, byte-identical resp. With it on, `_invoke` sends the
  same request plus `stream=True` + `stream_options={"include_usage": True}` (on a COPY of kwargs, so the
  caller dict is never mutated) and hands the chunk iterator to `_assemble_stream`.
- **Hand-rolled reassembly, not `litellm.stream_chunk_builder`.** `_assemble_stream(chunks)` folds the
  streamed deltas back into an attribute-shaped object equivalent to a non-streaming
  `resp.choices[0].message` + `resp.usage`: `content` and `reasoning_content` fragments are concatenated,
  and `tool_calls` are rebuilt by their delta `index` (id/name arrive once, `arguments` arrive split). Usage
  rides the terminal `include_usage` chunk (which carries no `choices`). It is hand-rolled specifically so
  the reassembly is unit-testable with a fake chunk iterator and no litellm dependency.
- **The equivalence contract.** The reassembled `msg` is consumed by: complete()'s dropped-call test
  (`not (msg.content or "").strip() and not (msg.tool_calls or [])`), `trajectory.log_model_call` (reads
  `tc.id` / `tc.function.name` / `tc.function.arguments`, `msg.content`, `getattr(msg,"reasoning_content")`,
  `usage.prompt_tokens/completion_tokens`), and the planner reasoning fold (planner.py:50). The `msg` is
  never re-serialized by litellm — the planner builds fresh assistant dicts from its attributes — so an
  attribute-accessible `SimpleNamespace` satisfies every consumer.
- **Only the primary turn streams.** `warm_up()` and `_summarize_once()` call `litellm.completion` directly
  and never stream — a warm-up probe and a compaction summary do not benefit and must stay simple.

## Acceptance

Each item is an assertion in `scripts/check_stream.py` (20/20, dep-free — a fake litellm is injected into
`sys.modules` before importing `src.model`).

- Reassembly: split `content`, index-fragmented `tool_call` name/arguments, and `reasoning_content` fragments
  rebuild to `msg.content`, `msg.tool_calls[0].id/.function.name/.function.arguments`, and
  `msg.reasoning_content`; the terminal usage chunk populates `resp.usage`; `finish_reason` is captured.
- Dropped-call: an all-empty-delta stream rebuilds to `content=None` + `tool_calls=None`, and complete()'s
  exact dropped test computes True.
- Flag OFF: `_invoke` returns `litellm.completion(**kwargs)` verbatim with NO `stream`/`stream_options` keys,
  and does not mutate the caller's kwargs dict (byte-identical request).
- Flag ON: `stream=True` + `stream_options={"include_usage": True}` are sent, a reassembled response is
  returned, and the caller's kwargs dict is still not mutated.
- `summarize()` does not stream even when `CODE_STREAM=True`.
- `SCHEMA_VERSION` is unchanged (`0.13.0`); `CODE_STREAM` is absent from `safety_fingerprint`.
- `CODE_STREAM` defaults False when unset.

## Non-goals

- No live token printing to the console yet — the delta iterator is fully consumed inside `_assemble_stream`
  before the message is returned; incremental display is a later, separate change.
- `warm_up()` and `summarize()` are deliberately not streamed.
- No change to the model_call record shape — streaming only changes how `msg`/`usage` are assembled, so
  `SCHEMA_VERSION` does not bump.

## Byte-identity

`CODE_STREAM` off (default) makes `_invoke` a pass-through to the original `litellm.completion(**kwargs)`, so
the request body, the consumed response, and every trajectory record are byte-for-byte what they were before
specs/0043 (verified: `check_effort` 21/21 unchanged; the OFF-path assertions in `check_stream.py`).

## Notes / open

- Depends on Together emitting a terminal usage chunk under `stream_options.include_usage`; if a provider
  omits it, `resp.usage` is None and `log_model_call` records empty usage for that streamed turn (tolerated,
  but token accounting for that turn is lost). The earlier live probe confirmed `reasoning_content` DOES
  stream for Inkling on Together; the usage chunk should be confirmed before relying on streamed accounting.
