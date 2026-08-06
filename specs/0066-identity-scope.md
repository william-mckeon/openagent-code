# 0066 — identity: answer when asked, never volunteer

Status: implemented
Flag: none new (scopes the specs/0063 `<model_information>` directive; rides `CODE_AGENT_IDENTITY_BLOCK` +
`CODE_PROMPT_HYGIENE`)

## Goal

Stop the agent from VOLUNTEERING its identity. Seen live: `Also — I am Arcus, created by Islander Intelligence.
I can build this out step by step once you confirm the direction.` — appended, unprompted, to a normal task
reply.

This is the over-correction from the identity fix (0063). The `<model_information>` block does two jobs, and we
only wanted one:

- **The job we wanted — redirect.** Inkling was trained to treat a `<model_information>` block as its
  authoritative identity, so injecting our own in that format makes the agent report *Arcus / Islander
  Intelligence* instead of *Inkling / Thinking Machines*. That worked (0063).
- **The job we inherited — announce.** The base model does not merely KNOW its identity from that block; it was
  trained to proactively STATE it. Speaking the format inherited the announce-reflex too, now pointed at our
  content — so it opens/closes unrelated replies with "I am Arcus, created by Islander Intelligence."

The counter-rule already exists and is ON: the `CODE_PROMPT_HYGIENE` note (0051/0061) says *"do NOT open a reply
by stating who you are… do not repeat the same self-description across turns."* It LOSES. That is the recurring
small-model ceiling — a **concrete, structural block in the format the model treats as authoritative beats a
soft "don't" rule sitting elsewhere in the prompt.** So the fix is not another soft rule; it is to co-locate the
constraint INSIDE the directive that is doing the pulling.

## Concepts

- **Scope the directive, don't add a rule.** `_identity_block`'s directive keeps its two working anchors — the
  when-asked clause (`answer consistently with the <model_information> block above`) and the terminal base-model
  ban (`…third-party model provider.`) — and gains a middle clause between them: *the block is REFERENCE only;
  do NOT volunteer, announce, or restate the identity; never open or close a normal reply with who you are;
  never append "I am {name}…" or the creator to a task answer; state it only in direct answer to an identity
  question.* The suppression now travels WITH the identity content, in the same authoritative structure, so the
  model can't obey the block while ignoring a distant soft rule.
- **Byte-identity anchor preserved.** The directive still ENDS at `…model provider.`, so the harness's
  block-excision (`OFF == ON minus the injected block`) needs no change; the when-asked phrase is preserved, so
  the existing directive assertion still holds. The only new surface is the middle clause.
- **Prompt-first, structure-if-it-survives.** This is a prompt fix against a small model. If a live run shows
  the reflex survived, the follow-up is a DETERMINISTIC strip (post-process the reply to remove an unprompted
  "I am {name}…" tail) — the structural backstop that finally beat the identity leak in 0063. Deliberately NOT
  in this spec; we don't add post-processing machinery we may not need.

## Acceptance

New assertion in `scripts/check_agent_identity.py` (dep-free, 9/9):

- Block ON: the directive is scoped — `REFERENCE only`, `do NOT volunteer`, `never append`, and `state it only
  in direct answer` all render.
- Every prior specs/0063 assertion still holds: the block + fields render, the when-asked directive and
  base-model ban are present, it sits right after the opening identity line, it names no base model, empty
  fields are omitted, and `OFF == ON minus the injected block` (byte-identity) — the excision anchor is
  unchanged because the directive still ends at `model provider.`.

## Non-goals

- **No deterministic strip.** Post-processing the reply to physically remove a volunteered identity tail is the
  reserved follow-up, gated on whether the prompt fix holds live.
- No change to the `CODE_PROMPT_HYGIENE` identity clause (0061) — it stays as reinforcement; the fix is in the
  block directive because that is the structure the model actually obeys.
- No new flag, no `SCHEMA_VERSION` bump, nothing added to `safety_fingerprint`.

## Byte-identity

With `CODE_AGENT_IDENTITY_BLOCK` off, `_identity_block` returns `''` — byte-for-byte unchanged. With it on, the
only difference is the scoping clause inside the directive; the block, fields, when-asked answer, and base-model
ban are all preserved. Verified: `check_agent_identity` 9/9, full dep-free suite green.
