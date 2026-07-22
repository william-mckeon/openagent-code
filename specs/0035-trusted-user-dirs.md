# 0035 — Trusted user directories (an explicit user path is a read grant)

Status: accepted
Flag: `CODE_TRUST_USER_DIRS` (default **off**) for parts A + B; part C tightens the existing
`CODE_SITUATIONAL_CONTEXT` feature.

## Goal

When a user *explicitly names an absolute directory*, the agent should be able to read it — without
the friction (and the failure modes) seen in a live session where a project the user named twice was
never reviewed. That log showed the real failure chain:

1. The user typed `C:\Users\willi\OneDrive\Desktop\OpenCode`.
2. The model **corrupted** it to `...\OpenCodeEnvironment` — bleeding the first word of the
   situational-context block, which `agent.py` injected as a `{"role":"user"}` message *adjacent to the
   task*, so the model treated the environment text as something the user typed and merged it in.
3. `request_dir` ran `os.path.isdir(<corrupted path>)` → `False` → "Not a directory", so the user never
   even saw a grant prompt. The agent flailed against the fence and gave up.

This spec closes that in three parts:

- **A (fix A)** — a directory the *user literally typed* in their REPL message, if it exists, is granted
  **read** access, keyed off the user's own text (immune to the model re-typing/corrupting it).
- **B (fix B)** — in **bypass** mode, at the top level, with a human at the REPL, `request_dir`
  **auto-grants** an existing directory instead of prompting `[y/N]` (bypass already means do-not-prompt).
- **C (fix C, the root cause)** — the situational-context env block is no longer injected as a sent
  `{"role":"user"}` turn adjacent to the task; it self-identifies as system state and is captured (not
  re-sent) so it can never again be attributed to the user or bleed into a path.

## Concepts

### The read-only tier (`Permissions.read_only_roots`)

The critical correction the design review surfaced: **`extra_roots` is not read-only.** The workspace
fence (`_within_roots`) is checked identically for read *and* write path tools, so a directory in
`extra_roots`, under bypass, is fully **writable** (`write_file`/`delete_file` pass the fence, then bypass
allows the mutation). Routing an auto-grant there would hand the agent write/delete over any project the
user named.

So A and B grant into a **new** `read_only_roots` list that **mutating** tools ignore:

- `_within_roots(abs, cwd, include_read_only=False)` — the extra `read_only_roots` are consulted **only**
  when `include_read_only=True`.
- The step-2 fence in `_decide_core` passes `include_read_only=not mutating`, so read tools
  (`read_file`/`grep`/`glob`/`tree`) reach a read-only-granted dir but `write_file`/`edit_file`/
  `delete_file` (and `apply_patch`/`run_command`, which the sandbox fences against `extra_roots` only)
  never do.
- `read_only_roots` starts empty and is populated only by an A/B grant (both flag-gated), so with the
  flag off it is empty and `_within_roots` is byte-identical to today.

### The extractor (`src/userdirs.py`)

Pure stdlib, no side effects, no `src` imports. Conservative by construction — **false-negative preferred
over false-positive** (the user can always `/add-dir`):

- **Anchored** token match — a drive-absolute `C:\...`/`C:/...` or a UNC `\\server\share\...`. A
  drive-*relative* `C:foo` is not matched. This is the anti-`OpenCodeEnvironment` guard: we take the
  user's literal token, we never greedily scrape or reconstruct a path.
- Strip surrounding quotes/brackets and trailing sentence punctuation.
- **Negation veto** — a token in a clause introduced by `not/don't/never/avoid/except/ignore/without/skip`
  is dropped ("don't touch `C:\Windows`" grants nothing).
- **`grantable_dir` safety filter** (also applied to fix B's model-supplied path) — reject a bare drive
  root / a UNC share root / a path shallower than `<drive>\<one component>`, and a **system/credential
  denylist** (`%SystemRoot%`, `Program Files*`, `ProgramData`, the user-profile *root* itself, and any path
  with a `.ssh`/`.aws`/`.gnupg`/`.git`/`.config` component) — **even if `isdir` is true**.
- Keep only `os.path.isdir`-true survivors; return `realpath`'d, de-duplicated dirs.

### Fix C — env block attribution

- `envcontext.build_env_context` header reframed to self-identify as auto-generated system state that is
  **not** a message from the user (every field line kept verbatim).
- `agent.py` keeps the pin (`set_env_context`, still `role:"user"` — a mid-array `system` message risks a
  Bedrock Converse rejection, so we do **not** flip the role) as the single **sent** copy, and replaces the
  duplicate `cm.add({"role":"user",...})` with a **capture-only** `cm.log_env_capture(env)` that logs the
  block to the trajectory as `role:"system"` and does **not** re-append it to the working set.
- Net effect: the model sees the env block **once** (a pinned block, before the working messages, no longer
  adjacent to the task) with a header that says it is not user input; the raw SFT stream captures it as
  system state, not a user turn.

## Acceptance (each is a check in `scripts/check_trust_dirs.py`)

1. Flag **on**, bypass, `interactive=True`, top level → `request_dir` grants an existing dir into
   `read_only_roots` **without** calling `ask`.
2. Flag **on**, a non-bypass mode → `request_dir` still calls `ask` (no silent auto-grant).
3. Flag **off**, bypass → the existing headless denial / interactive `[y/N]` prompt is byte-identical
   (auto-grant block skipped entirely).
4. Subagent (`depth>0`) in bypass → **no** auto-grant (a child can't self-widen the shared fence).
5. `user_typed_dirs` grants a plainly-named existing dir, but **not** a negated one, a denylisted one
   (`C:\Windows`, a `.ssh` dir), a bare drive root, or a non-existent path.
6. **Invariant:** a `write_file`/`delete_file` targeting a `read_only_roots` dir stays **denied** under
   both `bypass` and `acceptEdits`; a `read_file`/`grep` there is allowed.
7. `CODE_TRUST_USER_DIRS` defaults `False` when unset (opt-in), proven against the fallback, not this
   repo's live `.env`.

For fix C, `scripts/check_situational.py` gains a check that `log_env_capture` logs the env text with
`role:"system"`, does **not** append it to `working`, and leaves the pin (`pinned_env`) `role:"user"`.

## Traps (each folded into a guard above)

- **`extra_roots` looks read-only but isn't** → the whole point of `read_only_roots` (acceptance #6).
- **Greedy path scrape re-creates the corruption** → anchored token only, keyed off the user's literal text.
- **False grant from a mentioned path** → negation veto + denylist + `isdir` + realpath; read-only + top-
  level only. Dominant-intent is deliberately **not** required (a read-only, denylisted grant of a dir the
  user literally typed matches the user's stated intent; requiring intent would reject the very request).
- **Headless self-widening** → fix B requires `interactive` and `depth==0`; a headless/CI bypass run keeps
  the existing denial and must use `--add-dir`.
- **Bedrock mid-array `system` message** → fix C keeps the pin `role:"user"` and only reframes its header +
  moves the duplicate to a capture-only record.

## Non-goals (follow-ups, not this phase)

- A `/revoke` command + richer surfacing of the session-lived `read_only_roots` (this phase prints a
  conspicuous `auto-granted READ: <path>` line at grant time).
- Honoring a user-typed path on the headless `_one_shot` path (fix A lands in the REPL only).
- Re-stamping `safety_fingerprint` with `read_only_roots` / a resume-time fingerprint (specs/0033 non-goal).
- A guardian/PermissionRequest review seam for an auto-widen (`request_dir` is a read-only tool no reviewer
  sees today).

## Byte-identity

Flag off → `read_only_roots` is empty, `user_typed_dirs`/`grantable_dir` are never called, `request_dir`
falls through to today's code verbatim, and the fence is unchanged. Fix C runs only under
`CODE_SITUATIONAL_CONTEXT`, so a run with that feature off is untouched. `docker/code/Dockerfile` is a
**deliberate non-edit**: its ENV block pins only operational defaults and omits every default-off feature
flag; adding `CODE_TRUST_USER_DIRS` there would flip the in-image default.
