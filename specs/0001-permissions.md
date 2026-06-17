# Granular permissions (Phase 4 #6)

Port Claude Code's permission model into openagent-code: a real permission engine
in front of every tool call — **modes**, **allow/ask/deny rules**, and a
**workspace fence** — replacing today's single `CODE_AUTO_APPROVE` on/off switch.

## Goal

Today the only control over what the agent may do is one global boolean
(`CODE_AUTO_APPROVE`): gate every mutating tool, or gate none. And there is no
boundary at all on *where* it reads/writes — `_abs()` in `src/tools.py` honours
absolute paths and `..`, so the agent can touch files outside the workspace you
pointed it at.

This spec replaces that with the model Claude Code uses, so a run can be governed
the same way: pick a **mode** (how much to auto-approve), layer **rules** (allow
`pytest`, deny `rm`/`curl`, ask before `git push`), and **confine** file access to
the project directory (plus any dirs you explicitly grant). Deny always wins, so a
headless auto-run can still hard-block dangerous commands.

Hooks (programmable `PreToolUse`/`PostToolUse` gates) are the **second pass** — this
spec is the Core engine, and is designed so hooks slot in later without rework.

## Concepts

**Modes** (`CODE_PERMISSION_MODE`):
- `default` — mutating tools require approval: prompt if a human is present, else
  fall through to the rules/deny logic (no silent mutation).
- `acceptEdits` — `write_file`/`edit_file` auto-approved; `run_command` still gated.
- `plan` — read-only: every mutating tool is blocked. The agent may investigate and
  call `update_plan`, but changes nothing.
- `bypass` — everything auto-approved (today's `CODE_AUTO_APPROVE=true` behaviour).

**Rules** (`CODE_PERMISSIONS_CONFIG` → a JSON file, same pattern as `CODE_MCP_CONFIG`):
```json
{
  "deny":  ["run_command(rm:*)", "run_command(curl:*)", "read_file(.env)"],
  "ask":   ["run_command(git push:*)"],
  "allow": ["run_command(pytest:*)", "edit_file(src/**)"]
}
```
Matcher is `tool_name(pattern)`:
- **`run_command`** — `pattern` is a command prefix; trailing `:*` means "this prefix
  then anything" (`run_command(pytest:*)` matches `pytest -q`). Bare = exact. `*` = any.
- **file tools** (`read_file`/`write_file`/`edit_file`) — `pattern` is a path glob,
  workspace-relative (`edit_file(src/**)`).

**Workspace fence** (`CODE_ADD_DIRS`): every file-touching tool
(`read_file`/`write_file`/`edit_file`/`grep`/`glob`) is confined to the workspace
root plus any directories listed in `CODE_ADD_DIRS`. A resolved path outside all
allowed roots is blocked.

## Precedence (the crux — evaluate in this order, first match wins)

1. **deny rule matches** → **BLOCK**. Wins over everything, including `bypass` mode.
2. **path outside the fence** (file tools) → **BLOCK** (unless under workspace / `CODE_ADD_DIRS`).
3. **tool is read-only** (`read_file`/`grep`/`glob`) → **ALLOW** (it survived 1–2).
4. **mode is `plan`** → **BLOCK** (mutating tools are read-only-mode forbidden).
5. **ask rule matches** → **PROMPT** if interactive, else **BLOCK** (can't ask headless).
6. **allow rule matches** → **ALLOW**.
7. **mode baseline**: `bypass` → ALLOW · `acceptEdits` → ALLOW for write/edit, else
   prompt-or-block · `default` → PROMPT if interactive, else BLOCK.

## Acceptance (checkable)

- [ ] `CODE_PERMISSION_MODE=plan` → `write_file`/`edit_file`/`run_command` all blocked;
      `read_file`/`grep`/`glob` work; the run leaves the workspace byte-for-byte unchanged.
- [ ] `CODE_PERMISSION_MODE=acceptEdits` → an edit applies without prompting, but a
      `run_command` is still gated.
- [ ] **deny beats everything**: with `deny:["run_command(rm:*)"]` and mode `bypass`,
      `rm -rf x` is blocked; a non-denied command still runs.
- [ ] **deny beats allow**: a command matching both an `allow` and a `deny` rule is blocked.
- [ ] `ask` rule, non-interactive → blocked (recorded as such, not silently allowed);
      same rule, interactive → prompts the human.
- [ ] **Fence**: `read_file`/`write_file`/`edit_file` on an absolute path or a `../`
      path outside the workspace is blocked; adding that dir to `CODE_ADD_DIRS` allows it.
- [ ] **Back-compat**: with none of the new vars set, behaviour is unchanged —
      `CODE_AUTO_APPROVE=true` ⇒ `bypass`, `=false` ⇒ `default`; **eval still 13/13**.
- [ ] **Subagents** inherit the parent's mode + rules + fence and cannot exceed them.
- [ ] Every gated decision (allow/ask/deny + which rule/mode decided it) is recorded in
      the trajectory, so the flywheel captures *why* a tool call was permitted or refused.

## Non-goals (this pass)

- **Hooks** (`PreToolUse`/`PostToolUse` shell gates) — the second pass. The engine
  exposes a single `decide(tool, target, ctx)` seam so a hook layer wraps it later.
- **Layered settings hierarchy** (Claude Code's user/project/local `settings.json`
  precedence) — one `CODE_PERMISSIONS_CONFIG` file for now.
- **Mid-session mode switching UI** (a `/permissions` panel) — mode is set at start via
  env/flag. (A REPL `/mode <name>` command is a possible small follow-up.)
- **Network/egress sandboxing** beyond what `deny run_command(...)` rules express.

## Notes

- **Tool-name keys**: rules use openagent-code's real tool names (`run_command`,
  `edit_file`, …), not Claude's capability names (`Bash`, `Edit`). Same idea, grounded
  in this codebase's tools so there's no translation layer to get wrong.
- **The gate is at DISPATCH, not inside each tool** (refined during build): `src/agent.py`
  calls `permissions.decide(tool, args, ctx)` once before running any tool, rather than
  each tool calling `check()` itself. Two reasons it's better: (1) it's the single natural
  place to *capture* the decision (the `permission` record), and (2) it binds the decision
  to the **running agent's** trajectory — correct for subagents, which a traj-bound engine
  would mislog. The three in-tool `check()` calls in `tools.py` were removed; the engine
  resolves paths itself for the fence. `src/permissions.py` grows from a boolean into the
  engine behind one `decide()` method.
- **Headless-safe by construction**: because deny wins and `ask`/`default` block (not
  allow) when no human is present, an unattended run can never be *more* permissive than
  its rules — it can only refuse.
- **Why capture decisions**: a denied/asked tool call is training signal (the model
  learns the boundary), and an auditable record of what the agent was allowed to do.
