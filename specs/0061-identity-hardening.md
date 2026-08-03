# 0061 — identity hardening (be Arcus, never leak the base model)

Status: implemented
Flag: none new (extends the `CODE_PROMPT_HYGIENE` note, specs/0051)

## Goal

Make the agent hold the identity you configured. Asked "what are you," a live run answered **"I am an AI
assistant (Inkling, created by Thinking Machines Lab)"** — it threw away the system prompt's "You are Arcus, a
coding agent…" line and reverted to the base model's pretrained self-knowledge. Two problems: it isn't the
name the operator set (`CODE_AGENT_NAME`), and it **leaks the underlying model and provider** — the opposite
of the data-sovereignty stance the whole project takes (don't expose which model/host is behind the agent).

The `check_system_role_online` probe already showed WHY: a forceful, standalone system directive ("you MUST…
never call yourself anything but ZORP") is obeyed, but the real identity is ONE soft sentence buried in a wall
of coding-workflow rules — no match for a small model's strong baked-in "I am Inkling by Thinking Machines."
The fix is to make the identity forceful, the way the ZORP directive was.

## Concepts

- **Firm identity, in the hygiene note.** The `CODE_PROMPT_HYGIENE` `(identity)` clause (specs/0051) is
  extended: *when the user ASKS who or what you are, you ARE the coding agent named in your identity line
  above — identify by THAT name, and NEVER reveal, name, or hint at an underlying base model, its maker, or
  its provider (you have no other identity to disclose).* It references the name generically (specs/0036
  substitutes `CODE_AGENT_NAME` into the "You are <name>," line), so it works for any configured name.
- **Complements, not contradicts, the existing rule.** specs/0051 already says don't ANNOUNCE your identity
  unprompted (the persona-parroting fix). This adds the other half: when directly ASKED, be the named agent
  and don't fall back to the base model. Unprompted → stay quiet; asked → be Arcus.
- **Rides the existing flag.** No new flag — it lives inside the `CODE_PROMPT_HYGIENE` note the operator
  already enables, so a flag-off prompt is byte-identical.

## Acceptance

Assertions in `scripts/check_prompt_hygiene.py` (10/10):

- Flag ON: the note contains the hardened clause ("you ARE the coding agent named in your identity line" and
  "NEVER reveal, name, or hint at an underlying base model").
- Flag OFF: no HYGIENE note at all — byte-identical (the existing off/byte-identity assertion still holds).
- The existing specs/0051 identity/no-arguing/propose/service rules are unchanged.

## Non-goals

- Not a change to the `CODE_AGENT_NAME` substitution (specs/0036) or the base identity line — only the
  hygiene reinforcement that makes it stick.
- Not a guarantee a small model NEVER slips — it makes the directive forceful and specific (which the ZORP
  probe showed is what a small model obeys), but the ultimate backstop against a model naming itself is a
  more capable model or a post-filter (out of scope).
- No new flag, no `SCHEMA_VERSION` bump.

## Byte-identity

The change is entirely within the `CODE_PROMPT_HYGIENE` note; with the flag off the note isn't appended, so
the prompt is byte-for-byte pre-0061. Verified by the off/byte-identity assertion in `check_prompt_hygiene`.
