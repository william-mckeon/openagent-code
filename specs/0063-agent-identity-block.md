# 0063 — structured agent identity block

Status: implemented
Flag: `CODE_AGENT_IDENTITY_BLOCK` (default off)

## Goal

Make the agent report the identity you configured, by speaking the model's OWN identity format. A soft "You
are Arcus" line and a forceful hygiene rule (specs/0061) BOTH failed live — asked "who are you," the agent
still said "I'm Inky, an AI assistant created by Thinking Machines Lab." The reason isn't that the system
prompt is ignored (the reasoning-effort self-report from specs/0062 worked in the same turn): Inkling ships
with a strong, trained-in identity CONTRACT — a `<model_information>` block plus a "when asked about your
identity, answer consistently with the information above" directive — and the model treats THAT structured
format as authoritative. An ordinary prose line doesn't compete with it.

## Concepts

- **Speak the format the model honors.** When `CODE_AGENT_IDENTITY_BLOCK` is on, `build_system_prompt` injects
  a `<model_information>` block — the same shape Inkling's own identity uses — right AFTER the opening identity
  line, followed by the directive: *"answer consistently with the `<model_information>` block above. You are
  <name> — NEVER identify as, reference, name, or hint at any underlying base model, model family, or
  third-party model provider."* Populated with THIS agent, the model reports Arcus instead of Inkling.
- **Configurable fields.** `CODE_AGENT_NAME` (Name), `CODE_AGENT_OVERVIEW` (Overview), `CODE_AGENT_CREATOR`
  (Creator), `CODE_AGENT_CONTEXT` (Context window). Each is single-line + capped + re-sanitized on read (like
  the persona), and an EMPTY field is omitted from the block. The operator picks how much to disclose (the
  "full block" is Name + Overview + Creator + Context window).
- **Complements, not replaces.** The specs/0036 name substitution and the specs/0061 hygiene identity clause
  stay — they reinforce. This block is the primary mechanism because it's the format the model was trained to
  obey.

## Acceptance

Assertions in `scripts/check_agent_identity.py` (8/8, dep-free):

- OFF (default): no `<model_information>` block — byte-identical to the specs/0036 name-only prompt.
- ON: the block renders with Name / Overview / Creator / Context window, carries the "answer consistently"
  directive + the base-model ban, sits directly after the opening identity line, and contains NO base-model
  tokens (`Inkling` / `Thinking Machines` absent).
- Empty fields are omitted (only `Name:` renders when the others are blank).
- Byte-identity: OFF equals ON with the injected block excised.
- `CODE_AGENT_IDENTITY_BLOCK` defaults False when unset.

## Non-goals

- Not a guarantee a small model NEVER slips — the trained-in identity is strong; this uses the model's own
  authoritative format, which is the best in-prompt lever, but a determined slip would need a more capable
  model (the teacher question) or an output filter as a last-resort backstop.
- Not a scrub of the model's output (the earlier idea) — this fixes the cause (identity contract), not the
  symptom. An output scrub remains available as a separate belt-and-suspenders option if wanted.
- No `SCHEMA_VERSION` bump; not in `safety_fingerprint`.

## Byte-identity

`CODE_AGENT_IDENTITY_BLOCK` off (default): `_identity_block` returns `''` and nothing is injected, so the
prompt is byte-for-byte the specs/0036 build. Verified by the OFF / byte-identity assertions in
`check_agent_identity`, and the rest of the suite unchanged.
