# Natural host access (Phase 4 follow-up)

Make openagent-code work like a real local coding assistant: run it in a repo with a
clean command (no `CODE_*` env juggling), reference *other* folders on the machine,
let the agent *request* access when it needs it (human approves), and — critically —
never silently work on the wrong folder again. Builds directly on the #6 permission
fence (the safety substrate) and the local launcher.

## Goal

Two real problems surfaced in use:
1. **Env juggling made local use awful** — every run needed `$env:CODE_WORKSPACE=...;
   $env:CODE_MEMORY=...` etc. before `python -m src`.
2. **The Docker box + a workspace-only fence made cross-folder work impossible**, and
   worse — when asked to review a folder it couldn't see, the agent **silently reviewed
   the mounted workspace and labelled it as the requested repo** (a confident review of
   the wrong code). A grounding failure, not just a config gap.

This spec makes host access natural AND honest, with a clear split:
- **The human grants trust** (slash commands / flags) — they hold the keys to the fence.
- **The agent requests access** (a tool) — autonomous, but it must *ask*, never self-grant.

That split is the whole point: an agent that can silently widen its own fence has no
fence. Agentic *and* safe = the agent asks, the human approves.

## Already landed (prerequisite, done)

- **Local launcher** (`pyproject.toml`): `pip install -e .` → an `openagent-code`
  command; workspace defaults to the current directory.
- **Flags instead of env vars** (`src/cli.py` `_parse_flags`): `-C/--workspace`,
  `--mode`, `--add-dir`, `--memory/--no-memory`, `--warmup`.
- **The fence + `--add-dir`/`CODE_ADD_DIRS`** (#6): granting a reference folder already
  works at the permission layer (proven: workspace + granted dir reads; ungranted denied).

## This spec (remaining)

1. ✅ **DONE — `/add-dir <path>` and `/mode <name>` REPL commands** (human grants,
   mid-session): widen the live fence / switch mode without restarting. The
   `Permissions` object is shared, so appending to `extra_roots` takes effect on the next
   tool call. `/add-dir` also drops a note into the context so the agent *knows* the
   folder is now readable. (`src/cli.py` `_repl_add_dir`/`_repl_set_mode`; verified
   denied→allowed after grant.)
2. **`request_dir` agent tool** (agent requests): when the agent needs a folder outside
   its roots, it calls `request_dir(path, why)` → in interactive mode this prompts the
   human (reusing `ask_user` plumbing) → on approval the path is added to the live fence;
   on refusal (or headless) it's denied. The agent is autonomous but never self-grants.
3. **Grounding fix** (the important one): the agent must NEVER substitute the workspace
   for a path it cannot read. When a referenced path is outside the fence / missing, the
   fence-denial message is actionable ("`X` is outside your allowed dirs — call
   `request_dir` or ask the user to `--add-dir` it"), and the system prompt instructs:
   *if asked about a path you cannot access, say so explicitly — do not review a different
   folder and present it as the requested one.*
4. **`grep`/`glob` span granted roots**: searches optionally cover the workspace + added
   dirs (or accept an added-dir as their `path`), and the system prompt **advertises** the
   currently-granted reference dirs so the agent can use them naturally.

## Acceptance (checkable)

- [ ] `/add-dir <path>` in the REPL makes a previously-denied read of that path succeed on
      the next turn (no restart); `/mode plan` makes the next edit get denied.
- [ ] `request_dir(path)` in interactive mode prompts the human; on "yes" the path becomes
      readable, on "no" it stays denied. In headless mode it is denied (never auto-granted).
- [ ] Asked to review a path it cannot access, the agent **states it cannot access it**
      (and suggests `request_dir`/`--add-dir`) instead of reviewing the workspace.
- [ ] With a folder granted, `grep`/`glob` can search it, and the system prompt lists it
      as an available reference directory.
- [ ] An agent in `plan`/headless **cannot** widen its own fence (no self-grant path).
- [ ] eval still 13/13 (none of this is on by default; the fence default is unchanged).

## Non-goals (this pass)

- **Agent self-granting without approval** — explicitly forbidden; it defeats the fence.
- **Remote / SSH / URL directories** — local filesystem roots only.
- **Per-repo `.openagent/config`** (pin mode + add-dirs per project) — a clean follow-up
  that pairs with the memory file already at `.openagent/`, but not required here.
- **Replacing the native file tools with an MCP filesystem server** — the native tools
  (line-numbered reads, exact-match edits) are a tuned core asset and the training signal;
  MCP stays for genuinely external capabilities, not for re-wrapping file access.

## Notes

- **Why human-grants / agent-requests, not one tool**: keeps the security boundary real.
  The fence means something only if widening it requires a human in the loop.
- **Builds on #6**: every grant — flag, slash command, or approved `request_dir` — just
  appends to the same `Permissions.extra_roots`. One mechanism, three front doors.
- **Docker is unaffected**: it stays the sandbox/eval runtime; this is about the local
  interactive experience. In a container you still grant via mount + `CODE_ADD_DIRS`.
