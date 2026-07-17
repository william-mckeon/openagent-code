# 0023 — project todos (a persistent backlog the agent maintains)

## Goal
Give openagent-code a durable, per-project BACKLOG — the living "what's still to do" on this repo — that
the agent maintains and surfaces as it works, exactly like the working checklist a good assistant keeps.
It's the third leg beside two things that already exist but aren't this:

- **`update_plan`** is the CURRENT task's step checklist — ephemeral (reset every turn) and completion-gated
  (each completed step must be backed by a real file change). It's "the steps of this task," not the project.
- **memory** (`.openagent/memory.md`) holds durable FACTS about the repo — conventions, where things live —
  not actionable work.

Project todos is the durable, cross-session list of outstanding WORK. It persists to
`<workspace>/.openagent/todos.md` (co-located with memory), is reloaded and SHOWN at the start of every
session, and survives closing OAC. The agent records work it discovers and checks items off; you can edit
the file by hand.

## Concepts
- **The file** — a human-editable markdown checklist at `config.todos_file(workspace)` (default
  `.openagent/todos.md`), with three item states: `- [ ]` pending, `- [~]` in_progress, `- [x]` done. The
  parse is LENIENT (accepts `*` bullets, `[X]`, `[/]`, leading whitespace) so a hand-typed file still loads,
  and it round-trips (`parse(render(items)) == items`).
- **The store** (`src/todos.py`) — the memory analog in shape (`path`/`load`, per-workspace, never raises)
  but STRUCTURED: `parse`/`render`/`save` a checklist plus pure list transforms (`add`, `set_status`,
  `clear_done`, `outstanding`). It differs from memory in ONE load-bearing way — memory is append-only text,
  but flipping an item's status requires a READ-MODIFY-WRITE, so `save` rewrites the whole file. The prompt
  injection and startup display show only OUTSTANDING items (done items are history, not backlog).
- **The tool** (`project_todos`) — one registration-style tool with an `action` (`list` / `add` / `start` /
  `done` / `clear`), selecting an item by the NUMBER shown in the rendered list or its exact text
  (unique-or-refuse). It writes the file directly via `ctx.cwd`, like `remember`; it runs nothing else.
- **Config** — `CODE_PROJECT_TODOS` (default false), `CODE_PROJECT_TODOS_FILE` (`.openagent/todos.md`),
  `CODE_PROJECT_TODOS_MAX_CHARS` (an OUTER bound on the prompt injection, capped by WHOLE LINES — never a
  byte tail that would slice a `- [ ]` line). Loaded in `cli._load_todos`, threaded through
  `runtime.build_agent(todos=...)` into `build_system_prompt`, reloaded on `session.resume`.
- **Separate from update_plan** — the injected block and the tool note tell the agent to PROMOTE a backlog
  item into the current task's `update_plan` when it starts it, never to fold the two (folding would break
  update_plan's file-change completion gate). The agent AUTO-maintains the backlog; the user can also edit it.

## Acceptance
- `src/todos.py`: `path` / `parse` / `render` / `load` / `save` / `backlog_text` + `add` / `set_status` /
  `clear_done` / `outstanding`; own `_MARKS` (NOT `tools._PLAN_MARKS`); lenient `parse`; round-trips;
  `save` overwrites (structured), never raises on load.
- `src/config.py`: `PROJECT_TODOS` + `PROJECT_TODOS_FILE` + `PROJECT_TODOS_MAX_CHARS` + `todos_file()`
  (a clone of `memory_file`).
- `src/tools.py`: `project_todos` + `TODO_TOOLS`; writes via `src/todos.py` (no `_record_mutation`).
- `src/toolset.py`: offers `TODO_TOOLS` only when `config.PROJECT_TODOS` (the single offering site).
- `src/prompts.py`: a `todos=None` param + a `## Project todos` injection block (gated on a non-empty
  string) + a tool-presence-gated note teaching auto-maintain + promote-into-update_plan.
- `src/runtime.py` / `src/session.py` / `src/cli.py`: thread `todos=` through `build_agent`; load + SHOW the
  backlog at session start (fresh and resumed) and re-show it in the REPL only when it changed.
- `src/permissions.py`: NO change — `project_todos` stays OUT of `MUTATING` (non-mutating, works in
  plan/propose mode) and carries no path (the fence is a no-op).
- `scripts/check_todos.py` — dep-free, no model/network.
- **Flag OFF is byte-identical**: the tool isn't offered, nothing is loaded / injected / printed, no
  trajectory record changes.

## Traps (each is a test)
- **Don't reuse `_PLAN_MARKS`** — it's `[ ]`-bare and says "completed", the wrong form and status for a
  checkbox file; `todos.py` owns `_MARKS` and a lenient parser that round-trips and skips its own header.
- **Structured, not append-only** — `save` READ-MODIFY-WRITES; a byte-tail cap (as memory uses) would slice
  a checkbox line. Bound the prompt injection by WHOLE LINES / outstanding-only, never a byte tail.
- **Non-mutating** — `project_todos` must stay OUT of `permissions.MUTATING` (or it's blocked in
  plan/propose) and must NOT call `_record_mutation` (or the completion/grounding gates treat the tracker
  file as a project change).
- **Build-time injection, not a per-turn pin** — inject the backlog once in `build_system_prompt` (the
  memory analog), NOT via `cm.set_pinned` in the loop; pinning it re-lists the backlog every step and
  collides with the `update_plan` pin.
- **Separate from update_plan** — the note teaches PROMOTE, never fold; gate it on the tool's PRESENCE (not
  a mode/flag string) so a flag-off prompt is byte-identical.
- **Display vs load** — keep `_load_todos` (feeds the prompt) and `_show_todos` (prints for the user)
  separate so the REPL doesn't double-print; show only outstanding items, only when there are any.

## Non-goals (v1)
- Not a replacement for `update_plan` (per-task verified steps) — the backlog sits above it.
- No auto-extraction of todos from code (`# TODO` scraping) or from the conversation — the agent records
  them explicitly, like `remember`.
- No due dates / priorities / assignees — a flat checklist with three states.
- No cross-repo/global backlog — per-workspace, like memory.

## Notes
- Off by default (`CODE_PROJECT_TODOS=false`), like every adoption-track phase; documented in `.env.example`.
- `.openagent/` is already gitignored, so the backlog stays local unless a repo chooses to track it.
- The structured sibling of memory (facts) and update_plan (this-task steps): together they are "remember
  what I learned, track this task, and don't lose what's still left."
