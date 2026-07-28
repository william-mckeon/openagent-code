---
name: doc-style
description: Write or update a project's docs in the operator's house style — README + DATASHEET + numbered specs + commented .env.example + reasoned module docstrings.
---

Write documentation in THIS house style. Do not go review other repos to infer it — everything you need
is here. Apply it directly to whatever project you are documenting.

## The artifacts (each project has these)

- **README.md** — the front door. What it is in 1–2 lines; a quickstart (the exact install + run commands);
  a `src/` (or top-level) file tree where EVERY file gets a one-line `# what it does` comment; a config
  table `| VAR | default | one-line meaning |`; short sections for the real usage paths (local / docker /
  interactive). Optimized for getting running fast, not marketing.
- **docs/DATASHEET.md** — the reference "spec sheet". Numbered sections (`## 1. Overview`, `## 2. Command /
  Interface`, `## 3. Tools`, `## 4. Model / dependency`, `## 5. Configuration`, `## 6. Failure modes`).
  Dense tables: tools as `| tool | mutating? | description |`, config as `| VAR | default | description |`.
  State the ONE outbound dependency and the security/permission posture plainly. Reads like a datasheet.
- **specs/NNNN-slug.md** — one numbered design spec per substantive change (zero-padded, sequential; never
  backfill a gap). Fixed shape:
  - a title line `# NNNN — <slug>`, then `Status:` and (if any) `Flag:`
  - `## Goal` — what it does and WHY, naming the concrete failure it fixes
  - `## Concepts` — the mechanism + the load-bearing invariants
  - `## Acceptance` — a checklist where **each item is a concrete, checkable test** (it should map 1:1 to a
    real assertion in the test suite; write the traps as tests)
  - `## Non-goals` — what is deliberately out of scope, plus any recorded debt
  - `## Notes` / `## Byte-identity` — the discipline (e.g. "off by default → unchanged")
- **.env.example** — every config var with a multi-line `#` comment: what it does, WHY, its default, and the
  opt-in / byte-identical note. Group with `# --- section (Phase N / specs/NNNN) ---` headers. The commented
  value is the real default, so copying the file verbatim changes nothing.
- **ROADMAP.md** (project-level) — the phased plan: per phase, what it delivers and its risk.
- **Module docstrings** — every source file opens with a docstring: the file path, what it does, and WHY it
  exists (the recurring failure it prevents), plus the key invariant it upholds. Reasoned, not a restatement
  of the code.

## The voice (this is what makes it the style)

- **State the WHY, not just the what.** "The recurring failure was structural, not a prompt bug: a single
  agent in a single context window cannot hold a whole repo." Every non-obvious choice gets its reason.
- **Call invariants out explicitly** — "byte-identical when off", "one writer per file", "deny always wins".
- **Be honest about limits.** Name non-goals and known debt; never oversell. A partial fix is described as
  partial.
- **Dense and plain.** Short, information-rich sentences. Em-dashes for asides. No marketing adjectives, no
  filler, no emoji. Prefer a concrete example over an abstract claim.
- **Ground every claim in the real artifact.** Reference exact files, flags, and line numbers; do not
  describe a file you have not opened.

## How to apply it

Given a project, produce (or update) the artifacts above IN THIS STYLE, tailored to that project's domain —
its own services, tools, config, and failure modes. Match the project's existing conventions where they
already exist; fill the gaps with the shape above. When you write a spec, make the Acceptance items real
tests. When you touch `.env.example`, comment every var. When you write a module, open it with the reasoned
docstring. Report what you wrote; this is a documentation task — do not change application logic.
