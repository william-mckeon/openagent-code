# 0051 — prompt hygiene

Status: implemented
Flag: `CODE_PROMPT_HYGIENE` (default off)

## Goal

Close four behavioral failures a small model (Inkling-Small on Tinker) showed on a live Centpilot REPL run —
all of them promptable, none of them addressed by the base prompt today:

1. **Persona parroting.** With `CODE_AGENT_PERSONA="Arcus: sharp, direct, and quietly funny…"` appended to
   the system prompt every turn (prompts.py, specs/0036), the model restated its identity on nearly every
   reply ("Arcus — sharp, direct, quietly funny. Not a zombie."), spending its limited capacity announcing
   itself instead of working.
2. **Arguing with the user.** Asked "why do you keep repeating," it rebutted ("I'm not repeating — you are")
   across multiple turns instead of adjusting — a defensive loop that burned the session.
3. **Never proposing in propose mode.** The propose flow only unlocks when the model calls `propose_changes`
   (specs/0022/0048). The model instead fired `edit_file` / `run_command` directly, hit read-only denials,
   and kept retrying the raw op — it never connected "denied read-only" to "call propose_changes," and the
   existing guidance is soft and edit-only (a `docker restart` is run-shaped, not an edit).
4. **False "done" on a dead service.** It declared "plumbing fixed" while `curl localhost:8080` returned
   connection-refused — a runtime success claim with no successful runtime check behind it.

Plus a shell gap: the PowerShell 5.1 hints (specs/0046) missed `head`/`tail`, `$?`-as-exit-code, and
`tree -Depth`, all of which the model emitted and all of which failed on PS 5.1.

## Concepts

- **One gated note, four rules.** When `CODE_PROMPT_HYGIENE` is on, `build_system_prompt` appends a single
  `HYGIENE:` note covering: (identity) the persona is a STYLE to embody, never announced or restated;
  (no arguing) if the user says you repeated or misread, ADJUST — don't rebut; (propose recovery) in propose
  mode call `propose_changes` before ANY edit OR state-changing command (build/run/restart/deploy included),
  and if an op is denied read-only, propose it — do NOT retry the raw op; (service honesty) never claim a
  service/app is up/serving/"plumbed" unless you actually reached it this turn (e.g. an HTTP 2xx).
- **Additive + gated.** The note is the LAST thing appended to the accumulated `note` block, only under the
  flag, so an off build is byte-identical to the pre-0051 prompt. It strengthens — never replaces — the
  existing propose guidance (specs/0022) and the "never claim a check passes unless you ran it" rule, which
  stay in the base prompt unchanged.
- **Shell-hint gaps folded into the existing block.** The specs/0046 PowerShell rules line gains
  `head`/`tail` → `Select-Object -First/-Last`, `$?` is a BOOLEAN (use `$LASTEXITCODE` for a native exit
  code), and a tree view is `Get-ChildItem -Recurse` (`tree` has no `-Depth`). This lives inside the existing
  `CODE_SHELL_HINTS` gate, so an off/non-Windows block is still byte-identical; the additions are only new
  substrings in the same one-line rule.
- **Prompt-first, not a guarantee.** These raise the odds a weak model behaves; they do not force it. The
  structural backstops (specs/0052 propose first-approval affordance, specs/0053 runtime-done honesty gate)
  are what make correctness independent of the model complying.

## Acceptance

Each item is an assertion in `scripts/check_prompt_hygiene.py` (dep-free, no model), plus the extended
shell-rule assertion in `scripts/check_situational.py`.

- Flag OFF (default): the system prompt contains no `HYGIENE:` note (byte-identical).
- Flag ON: the note is present and carries all four rules (identity / no-arguing / propose-recovery /
  service-honesty), each asserted by a distinctive substring.
- Byte-identity: the OFF prompt equals the ON prompt with ONLY the `HYGIENE:` note excised.
- `CODE_PROMPT_HYGIENE` defaults False when unset (opt-in).
- Shell hints ON + PowerShell: the rules line now includes `Select-Object -First` and `$LASTEXITCODE`
  (head/tail + `$?` gaps) alongside the existing mappings.

## Non-goals

- Not a fix for the persona line itself — `CODE_AGENT_PERSONA` stays the user's config; the note tells the
  model to embody it silently rather than trimming it. Trimming is an optional operator tweak.
- Not the structural propose or honesty fixes — those are specs/0052 and specs/0053. This spec cannot make
  a non-complying model call `propose_changes`; it only tells it to.
- No new tool, no toolset change, no `SCHEMA_VERSION` bump.

## Byte-identity

`CODE_PROMPT_HYGIENE` off (default) short-circuits the note append, so the system prompt is byte-for-byte the
pre-0051 build; the shell-hint additions ride inside the already-gated `CODE_SHELL_HINTS` block. Verified:
`check_situational` still passes with the extended assertion, `check_prompt_hygiene` asserts the off/on
byte-identity, and the flag is not added to `safety_fingerprint`.
