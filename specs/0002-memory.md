# Cross-session memory (Phase 4 #7)

Persistent, per-project understanding that survives between runs. The agent writes
durable notes about *your* repo with a `remember` tool, and those notes are loaded
back into context at the start of every later session — so it accumulates knowledge
of your codebase over time instead of starting cold each run.

## Goal

Today every run starts from zero: the agent re-learns the repo each time, and nothing
it figures out (build quirks, where things live, conventions, gotchas) carries to the
next session. Claude Code solves this with a project memory file (`CLAUDE.md`) that is
auto-loaded into context. This spec ports that idea, minimally and on this codebase's
terms: a per-project memory file the agent can append to (`remember`) and that we load
into the system prompt at session start.

Memory is **opt-in** (`CODE_MEMORY`, default off). Two reasons: it writes a file into
the target repo (a side effect the user should choose, consistent with the web-tools
opt-in), and it must stay OFF for `eval` so the harness remains isolated and reproducible.

## Concepts

- **The memory file** — `<workspace>/.openagent/memory.md` by default
  (`CODE_MEMORY_FILE`, resolved relative to the workspace). Per-project automatically,
  because it lives in the repo. Human-readable markdown.
- **`remember` tool** — appends a timestamped note to the memory file. Available only
  when `CODE_MEMORY` is on (added to the active toolset like the web tools). It is the
  agent's own notebook, NOT a project edit, so it is treated as **non-mutating** for
  permission gating (works even in `plan` mode), while still inside the workspace fence.
- **Loading** — at session start (one-shot, REPL, and resume), `memory.load(workspace)`
  reads the file, caps it to `CODE_MEMORY_MAX_CHARS`, and the text is appended to the
  system prompt under a `## Project memory` heading. Because the system prompt is logged
  as the first raw `turn`, the trajectory still captures exactly what the model saw
  (the Phase-3 self-containment gate holds).

## Acceptance (checkable)

- [ ] `memory.load(ws)` on a workspace with no memory file returns `""` (no error).
- [ ] After `memory.remember(ws, "note A")`, `memory.load(ws)` contains "note A".
- [ ] A second `remember(ws, "note B")` APPENDS — `load` returns both A and B.
- [ ] `load` truncates to `CODE_MEMORY_MAX_CHARS` (keeping the most recent content).
- [ ] Two different workspaces do not share memory (per-project isolation).
- [ ] With `CODE_MEMORY=true`, `remember` is in `active_tools()`; with it off, it is absent.
- [ ] `build_system_prompt(mode, tools, memory="X")` includes "X" under a memory heading;
      with `memory=None` the prompt is byte-for-byte the pre-memory prompt.
- [ ] `remember` is permitted in `plan` mode (non-mutating), and its write lands inside
      the workspace (fence-respecting).
- [ ] **eval stays 13/13**: memory defaults off, so the harness builds the agent without
      it and remains isolated/reproducible.

## Non-goals (this pass)

- **Automatic memory extraction** — the agent decides what to remember via the tool; we
  don't auto-mine trajectories into memory.
- **Summarization / pruning** of the memory file — it appends; `load` just caps on read.
  (A compaction pass for memory is a follow-up.)
- **Hierarchy** (user/global vs project) — one per-project file for now.
- **Semantic retrieval** — memory is a flat injected file, not an embedding search.
- **A `recall` tool** — unnecessary; memory is already in context. (Reconsider if the
  cap forces selective loading later.)

## Notes

- **Opt-in keeps eval clean** without special-casing the harness: `CODE_MEMORY` is off by
  default, so `active_tools()` omits `remember` and `cli`/`session` pass no memory text.
- **Subagents stay lean**: they build via `build_agent` WITHOUT memory, so the full file
  isn't re-injected into every child context.
- **Capture**: a `remember` call is a normal `tool_call` (captured); the loaded memory
  rides in the system-prompt `turn`. So no new trajectory record type is required and the
  schema does not bump for this feature.
- **Why a tool, not auto-save**: an explicit `remember` makes "what the agent chose to
  persist" a learnable, auditable signal — consistent with the flywheel.
