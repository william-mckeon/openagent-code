# 0007 — Verified completion

> "Done" is a state the harness **confirms against the filesystem**, not one the model
> **declares**. Phase 6 of the ROADMAP.

## Why

A live review-then-edit run reported *"all suggested files have been updated"* and *"all changes
have been saved and verified"* — while git showed the recommended deletes never happened and the
"comprehensive" doc it described was never written. The model **claimed work it did not do.** The
system prompt already forbids this ("never claim an action you did not perform; don't claim success
without evidence") and the model ignored it — so a prompt rule alone can't close it. The harness has
to *check*.

Two enablers made the lie easy, both now removed: `edit_file` reported success on a no-op
(`old==new`), and there was **no sanctioned way to delete a file** (`rm` is denied, no delete tool),
so a delete task could only ever be faked.

## Design

- **Mutation ledger.** Every successful `write_file` / `edit_file` / `delete_file` records its
  workspace-relative path and action on `ctx.mutations` (`{path: "write"|"edit"|"delete"}`), fresh
  per agent (subagents included).
- **Plan steps name their file.** `update_plan` steps take an optional `file`; the structured plan
  is kept on `ctx.plan_items` alongside the pinned text.
- **The completion gate.** When the model finishes a turn with no tool call (declares done), the
  harness computes the *unverified* steps: any step marked `completed` whose named `file` has no
  matching entry in `ctx.mutations`, or a delete whose file still exists, or an edit whose file is
  gone. If any exist and the re-prompt budget remains, the loop **does not accept "done"** — it
  appends the discrepancy and lets the model fix it. When the budget is spent, it returns an honest
  **`unverified_completion`** outcome.
- **`delete_file` tool.** The sanctioned, verifiable removal path. Fenced + permission-gated like
  write/edit; `delete_file(.env)` and `delete_file(.git/**)` are denied.

Steps without a named `file` can't be checked per-item and are trusted — the gate never produces a
false positive on a pure review (no completed file-steps → nothing to verify), so reviews and
read-only work are unaffected.

## Config

- `CODE_VERIFY_COMPLETION` (default `true`) — the gate on/off; off restores accept-on-first-final.
- `CODE_VERIFY_COMPLETION_RETRIES` (default `2`) — how many times the harness challenges a
  mismatch before recording `unverified_completion`.

## Files

- **[src/tools.py](../src/tools.py)** — `ctx.mutations` / `ctx.plan_items`; `_record_mutation`;
  `delete_file` tool + registry; `update_plan` `file` support.
- **[src/agent.py](../src/agent.py)** — the completion gate at the final-answer point;
  `_unverified_items` / `_completion_challenge`.
- **[src/config.py](../src/config.py)** — the two knobs.
- **[src/prompts.py](../src/prompts.py)** — "completion is verified, not declared" discipline.
- **[src/permissions.py](../src/permissions.py)** / **[permissions.json](../permissions.json)** —
  `delete_file` fenced + gated; `.env`/`.git` deletion denied.
- **[src/cli.py](../src/cli.py)** / **[src/subagent.py](../src/subagent.py)** — classify
  `unverified_completion` as a distinct, non-success outcome.
- **[.env.example](../.env.example)**, **[ROADMAP.md](../ROADMAP.md)**,
  **[scripts/check_permissions.py](../scripts/check_permissions.py)** — docs + acceptance.

## Acceptance (checkable)

- [ ] A run that marks a plan step `completed` with a `file` it never changed is **challenged**,
      not accepted; after the budget it ends `unverified_completion` (verified offline, no model).
- [ ] `delete_file` removes a file, records the deletion, and the gate confirms it's gone;
      `delete_file(.env)` / `delete_file(.git/**)` are denied even under bypass; the fence holds.
- [ ] `edit_file` rejects `old==new`.
- [ ] A pure review (no completed file-steps) is unaffected — the gate finds nothing to verify.
- [ ] `unverified_completion` is dropped by `train/convert.py` (not in `KEEP_OUTCOMES`).
- [ ] `CODE_VERIFY_COMPLETION=false` restores the old behavior.

## Non-goals (this pass)

- **Harness-derived task lists** (parse the user's request into a checklist). v1 relies on the
  model maintaining `update_plan`; the prompt mandates it for file tasks.
- **NL claim parsing** (verify the free-text final answer). Too unreliable; the gate is anchored on
  the structured plan + the mutation ledger instead.
- **Scoring false completion in the eval** — that's Phase 8 (`eval/rubric.py` + a new agentic task).
  Phase 6 only *records* the outcome so Phase 8 can select against it.
