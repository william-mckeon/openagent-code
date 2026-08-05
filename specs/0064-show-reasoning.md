# 0064 — stream the reasoning to the REPL

Status: implemented
Flag: `CODE_SHOW_REASONING` (default off)

## Goal

Let you watch the model think. It produces a separate reasoning channel (`reasoning_content`) that is already
captured in the trajectory, but the REPL only prints the final answer — so the reasoning is invisible live.
The user wanted to see it as it happens (an earlier run tried to fake it by narrating status through
`run_command`). Streaming is already on (specs/0043) and its assembler already separates the reasoning deltas,
so this is display-only: tee that stream to the console.

## Concepts

- **Tee the reasoning stream, don't change it.** `_assemble_stream(chunks, show_reasoning=False)` gains the
  flag; when on, each `reasoning_content` delta is printed to the console as it arrives — a dimmed
  `thinking: …` stream, opened on the first delta and closed (style reset + newline) at the end, so the
  answer prints clean below it. The reassembled response object is byte-identical either way; the reasoning is
  still captured in the trajectory regardless.
- **Top-level REPL only, by construction.** The flag rides down `Model(show_reasoning=…)` ← `build_agent(…)`,
  which DEFAULTS it to False. Only the interactive entry points (`cli` REPL / one-shot, `session` resume) pass
  `config.SHOW_REASONING`; subagents (`subagent.run_subagent`) and eval call `build_agent` WITHOUT it, so the
  grounding verifier, guardian, and eval runs stay silent — no screen flood.
- **Needs streaming.** The live token-stream only happens on the `CODE_STREAM` path (the reasoning arrives as
  deltas). With streaming off there is nothing to tee live.

## Acceptance

New assertions in `scripts/check_stream.py` (25/25, dep-free via the injected fake litellm + stdout capture):

- `show_reasoning=True`: `_assemble_stream` tees the reasoning to stdout (the text + a "thinking" marker).
- `show_reasoning=True`: the reassembled response is UNCHANGED (content + reasoning_content identical) —
  display-only.
- `show_reasoning=False` (default): nothing is printed (byte-identical to specs/0043).
- `Model(show_reasoning=…)` stores the flag; default False.
- `CODE_SHOW_REASONING` defaults False when unset.

## Non-goals

- Not streaming the ANSWER live — only the reasoning is teed; the answer still prints whole below it (a
  bigger change, left out).
- Not a per-turn `/thinking` toggle command — just the flag (could be added later).
- Not a change to reasoning CAPTURE (trajectory.py already logs `reasoning_content`) or to any downstream
  consumer (dropped-call check / log_model_call / planner fold are untouched).
- No `SCHEMA_VERSION` bump; not in `safety_fingerprint`.

## Byte-identity

`CODE_SHOW_REASONING` off (default): `build_agent` builds the Model with `show_reasoning=False`,
`_assemble_stream` prints nothing, and every consumer sees the same reassembled object — byte-for-byte
specs/0043. Subagents/eval never pass the flag, so they are unconditionally silent. Verified by the OFF /
display-only assertions in `check_stream` and the full suite unchanged.
