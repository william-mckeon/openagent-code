# Changelog

Point-in-time record of substantive changes, so the live README / DATASHEET / ROADMAP can describe the
*current* state without carrying history. Each numbered spec (`specs/NNNN-*.md`) remains the authoritative
per-change design record; this is the index. Newest first.

## Model runtime

The inference model is an env-boundary (`CODE_MODEL` / `CODE_API_BASE` / `CODE_API_KEY`), so every move
below was a `.env` change, never a code change:

- **Inkling-Small on Thinking Machines' Tinker** *(current)* — `CODE_MODEL=openai/thinkingmachines/Inkling-Small:peft:262144`,
  Tinker's OpenAI-compatible endpoint, 256k window. Native tool-calling + streaming confirmed; `reasoning_effort`
  accepts `low`..`xhigh` (xhigh = 0.99 = max); `separate_reasoning` defaults on, so `reasoning_content` is returned.
- **thinkingmachines/Inkling on Together** — `https://api.together.xyz/v1`, OpenAI-compatible.
- **gpt-oss-120b on AWS Bedrock / self-hosted vLLM (RunPod)** — the original baseline; last measured eval 13/13.

## Features (specs/0022–0074)

- `0074` **resume integrity** — six resume-cluster fixes, incl. two that PERMANENTLY broke a session:
  `sanitize_tail` is now a full-history tool-pairing scan (a MID-list / partial-results dangling `tool_use` is
  stubbed or dropped instead of poisoning every later step); `--resume` no longer crashes on a truncated last
  JSON line (`_load_records` skips it); ALL stale `role:'system'` env blocks are filtered on rehydrate (not
  just a leading one); mid-session `/add-dir`/trusted-dir grants are persisted as typed `dir_grant` records and
  replayed by tier; and `JsonPlanner`'s nudge budget resets per task. No new flag; session.py's model imports
  made lazy so its pure helpers stay dep-free.
- `0073` **gate-honesty** — six corpus-poison / gate-bypass fixes: `ran_check` no longer flips "verified" on
  `mkdir build`/`git checkout build`/`npm run dev` (matches only real tools / `<tool> <verb>`); `ran_healthcheck`
  no longer flips "service up" on any URL like `git clone https://…` (only a real probe tool counts); a
  timed-out verifier now records a FAILED check instead of a PASS; grounding sees Windows `src\main.py`
  citations (backslash added to `_QUOTED`); the scrubber redacts UNQUOTED `.env`/export secrets
  (`API_KEY=…`, `password: …`); and the card regex requires the grouped 4-4-4-4 form so a 16-digit epoch/id no
  longer false-matches. No new flag.
- `0072` **log-review fixes** — four NOVEL bugs a full `logs/*.log` review surfaced: (N1) a missing required
  tool arg leaked a raw `KeyError` to the model — `Registry.run` now validates schema `required` args and
  returns "missing required argument: 'path'"; (N2) a subagent under propose mode was deadlocked (can't mutate,
  can't approve) with a misleading deny — the propose deny is now depth-aware and tells a child to report up;
  (N3) Windows `run_command` output mojibake (cp1252 decoded as utf-8) — a UTF-8 prelude forces PowerShell
  output to match the decode; (N4) `2>&1` on a native exe flips the exit code so success reads `[FAIL]` — a
  `CODE_SHELL_HINTS` clause warns against it. No new flag.
- `0071` **security-boundary hardening** — four verified bug-hunt findings, all default-on: (1) `print_tree`
  escaped the workspace fence because its `→tree` alias resolved AFTER the permission gate — now canonicalized
  before it (`permissions._canonical_tool`); (2) the self-kill guard was bypassed by the idiomatic
  `Get-Process python | Stop-Process` — now checked per-statement (kill-verb AND `python` token in either
  order, pipe stays in-statement), also catching `taskkill /IM python3.exe`; (3) `/add-dir` said "granted
  (read)" but appended to write-capable `extra_roots` — now routes to `read_only_roots` so writes are denied;
  (4) the goal entry filter missed `python3.12 -c` / `pythonw` / `nodejs -e` — now matched via a regex. No new
  flag.
- `0070` **REPL crash honesty** — the CRITICAL bug-hunt finding: a REPL turn that crashed mid-run (a Bedrock
  503) logged no `turn_outcome`, so a crash-only session was stamped `session_end='completed'` and
  `train/convert.py`'s legacy branch trained the truncated partial turn as a success (corpus poison). The
  except branch now stamps the turn `error` (written directly, not via `classify`, which would wash it to
  `completed`), routing convert to the per-turn path that drops it — one fix closing three findings (the legacy
  misroute and the `to_rows` counter skew too). Also: `Ctrl-C` mid-turn (a `BaseException`) now ends the turn
  and returns to the prompt instead of killing the whole REPL with a traceback. No new flag.
- `0069` **narration detector: quoted text** — closes the hole that let a SECOND live narration loop through
  the 0067 guard: punctuation inside the quoted message (`;`, `|`, `>`, `$?`) misread as shell operators,
  resetting the consecutive streak so the guard never fired. Operators now only count OUTSIDE the quoted
  spans; `$(` stays fatal anywhere (a PowerShell subexpression executes even inside double quotes);
  unbalanced quotes err conservative (guard doesn't fire). No new flag.
- `0068` **volunteered-identity strip — WITHDRAWN** — post-filtered a volunteered "I am Arcus, created by …"
  out of the final answer. Withdrawn same-week at the operator's direction: identity is never to be
  scrubbed/stripped from output — it's fixed at the model-format level (0063 block + 0066 scoping, which the
  next live run confirmed holds on its own). Code, flag, and harness removed; specs/0068 records the design
  and the withdrawal.
- `0067` **narration-stall guard** — `CODE_GUARD_NARRATION_STALL`: a weak model that finished but won't END the
  turn fills dead air with side-effect-free `run_command('Write-Output "Status…"')` — dozens in a row, burning
  steps and poisoning the corpus (seen live on a review that just needed to ask "which next?"). N consecutive
  narration-only steps trip a bounded nudge to finalize, then an honest new `narration_stall` outcome (added to
  `GATE_OUTCOMES`, so it's never washed to completed). Conservative detector: a read/pipe/redirect/chain never
  trips it. Cousin of the greenfield guard (0058) and the text-repetition degeneracy guard.
- `0066` **identity scope** — the specs/0063 `<model_information>` directive is scoped so the agent states its
  identity ONLY in direct answer to an identity question and NEVER volunteers it. Fixes a live over-correction
  where the block's inherited announce-reflex appended "Also — I am Arcus, created by Islander Intelligence" to
  ordinary replies — the soft `CODE_PROMPT_HYGIENE` "don't announce" rule kept losing to the concrete block, so
  the constraint (REFERENCE only; never open/close/append the identity) now lives INSIDE the directive itself.
  Byte-identity anchor unchanged (directive still ends at `…model provider.`). No new flag; a deterministic
  strip is the reserved follow-up if the prompt fix doesn't hold live.
- `0065` **read-only integrity** — the completion gate (specs/0007) no longer traps a READ-ONLY review into
  fabricating file changes. A plan step whose `file` is a directory (`Centpilot`) or a bare placeholder
  (`N/A`, `TBD`) is not a file mutation, so it can never be "not backed" — challenging it was unsatisfiable and
  escalated the agent into writing junk files to feed the gate. `agent._is_checkable_target` gates the
  challenge to REAL file targets (an existing file, or a path with an extension), so an edit-that-didn't-land
  and a create-that-never-happened still flag while a review step does not. The challenge text now offers the
  read-only exit (drop the file with `update_plan`; never create/edit/delete to satisfy the check), a
  `CODE_PROMPT_HYGIENE` `(read-only integrity)` clause forbids fabrication-to-satisfy, and the sibling
  `_unapplied_manifest` gets the same directory guard. No new flag. Caught live via `CODE_SHOW_REASONING`.
- `0064` **show reasoning** — `CODE_SHOW_REASONING`: tee the model's separate reasoning channel to the console
  live (a dimmed "thinking" stream above the answer) so you can watch it reason. Top-level REPL only (subagents
  / eval stay silent by construction); needs `CODE_STREAM`; display-only (reasoning is already in the trajectory).
- `0063` **agent identity block** — `CODE_AGENT_IDENTITY_BLOCK`: inject a structured `<model_information>`
  block (Name / Overview / Creator / Context window) in the SAME format the base model treats as
  authoritative, plus an "answer consistently with the above; never name an underlying model/provider"
  directive — so the agent reports Arcus instead of "Inkling, created by Thinking Machines." Fixes the cause
  (the trained-in identity contract) where the soft line (0036) and hygiene rule (0061) both failed.
- `0062` **context self-state** — `CODE_CONTEXT_SELF_STATE`: append the agent's current reasoning effort
  (`config.display_effort()`, e.g. `xhigh`) to the per-turn situational block, so when asked "what reasoning
  level are you at" it reports the real value instead of confabulating. Model id deliberately omitted (0061).
- `0061` **identity hardening** — extends the `CODE_PROMPT_HYGIENE` identity clause so that when asked who/what
  it is, the agent identifies as its configured name (Arcus) and NEVER reveals the underlying base model or
  provider. Fixes a live "I am Inkling, created by Thinking Machines Lab" — a name-and-sovereignty leak. No
  new flag.
- `0060` **effort-pin precedence** — adaptive effort (specs/0021, capped at `high`) no longer silently
  DOWNGRADES a pinned reasoning pass-through it can't represent (`CODE_REASONING_VALUE=xhigh`): when a
  non-ladder value is pinned, the escalation block is skipped so `xhigh` is applied every turn instead of
  being overridden to `high` the moment the run struggles. No new flag.
- `0059` **trajectory scrubbing** — `CODE_SCRUB_TRAJECTORY`: scrub secrets (private keys, JWTs, provider API
  keys, bearer/CSRF/session tokens, emails) and financial PII (currency amounts, account IDs, cards/SSNs)
  from each trajectory record at the single write choke point, before it hits disk — so a pasted budget or
  session token never enters the training corpus. Persisted copy only; the live agent is unaffected.
- `0058` **greenfield absence** — on a STRICTLY-empty workspace, an absence claim ("the workspace is empty",
  "no X here") no longer spawns the Tier-2 verifier (it's trivially true), killing the re-listing loop where
  the empty-Centpilot run re-ran `Get-ChildItem` turn after turn. A non-empty scaffold still verifies. Rides
  `CODE_GROUND_SKIP_GREENFIELD`; no new flag.
- `0057` **interactive guardian** — `CODE_GUARDIAN_INTERACTIVE`: the AI guardian adjudicates ask-tier commands
  in the REPL too — auto-approves the clearly-safe, on-request ones (a curl health-check, node -c, a build
  step), and defers anything it won't clear to the human `[y/N]`. Deny-rules / fence / mass-destruction cap /
  self-kill guard still apply. Makes "verify it runs" flow without a prompt for every probe.
- `0056` **runtime-done broaden** — widens the `CODE_VERIFY_RUNTIME_DONE` net to catch "Centpilot runs",
  "verified running", and "deploy fixed" (workspace-name subject + verified/deploy vocabulary) that 0053
  missed on a live run. No new flag.
- `0055` **non-interactive shell** — `CODE_SHELL_NONINTERACTIVE`: `run_command` runs PowerShell with
  `-NonInteractive` and the child's stdin as DEVNULL, so a command that reads stdin (a bare `echo` /
  Read-Host / a foreground prompt) fails fast instead of HANGING the REPL. Paired shell-hint against bare `echo`.
- `0054` **auto-window probe UA** — `_fetch_context_length` sends a browser `User-Agent` so the
  `CODE_MODEL_MAX_TOKENS=auto` context-window probe isn't 403/1010-blocked by Cloudflare on Tinker (which is
  why "auto window unresolved" kept the 131072 fallback). No new flag; pinning the window is still preferred.
- `0053` **runtime-done honesty** — `CODE_VERIFY_RUNTIME_DONE`: flag a "service is up / serving / plumbed"
  claim when no health-check (curl / http-get / port probe) returned ok this turn; the runtime twin of the
  unverified-success net. Closes a live false "Done — plumbing fixed" while `curl :8080` was refused.
- `0052` **propose first-approval backstop** — `CODE_PROPOSE_AUTOPLAN`: a read-only deny in propose mode
  becomes an interactive "approve + unlock? [y/N]", plus a `/approve` REPL command, so propose is never a
  dead-end when the model doesn't call `propose_changes`. Under deny-rules + the fence.
- `0051` **prompt hygiene** — `CODE_PROMPT_HYGIENE`: one system-prompt note — persona is a silent style, no
  arguing with the user, propose-first-with-recovery, service-up honesty — plus the PowerShell 5.1 shell-hint
  gaps (`head`/`tail` → `Select-Object`, `$?` → `$LASTEXITCODE`, `tree` has no `-Depth`).
- `0050` **self-preservation** — `CODE_GUARD_SELF_KILL`: hard-deny a name-based process kill that would end
  the agent's own `python` process (e.g. `Stop-Process -Name python`), in every mode incl. bypass.
- `0049` **extra-body** — `CODE_EXTRA_BODY`: merge arbitrary JSON params into the request `extra_body`.
- `0048` **propose follow-through** — `CODE_PROPOSE_RUN/EXTEND/PERSIST_AFTER_APPROVAL`: run/test, prompted
  extend, or scoped-bypass after a manifest approves, without re-proposing.
- `0047` **grounding early-stage** — `CODE_GROUND_GREENFIELD_MAX`: treat a small early scaffold as greenfield
  so grounding stops flagging build-session stub files.
- `0046` **shell-hints** — `CODE_SHELL_HINTS`: PowerShell 5.1 command rules in the env block on Windows.
- `0045` **auto-max-tokens** — `CODE_MODEL_MAX_TOKENS=auto` + optional per-request output cap.
- `0044` **reasoning-control** — `CODE_REASONING_PARAM/VALUE/TOPLEVEL`: pass-through for any reasoning param.
- `0043` **streaming** — `CODE_STREAM`: stream the primary model turn, reassembled to an equivalent response.
- `0042` **launch-flag validation + greenfield guard** — reject a mistyped `--mode`; `CODE_GROUND_SKIP_GREENFIELD`.
- `0038`–`0040` **workflows** — multi-phase engine, bounded parallel fan-out, async background runtime.
- `0035`–`0037` **trusted user dirs**, **agent naming** (`--set-name`/`--remove-name`), `$PROFILE` register.
- `0022`–`0034` **propose mode**, project todos, web tools, declared-done family, spec-first, skills,
  compaction-window safety, and the situational/attribution fixes. (Adoption-track `0012`–`0021` predate this
  window; see ROADMAP.)

All feature flags default OFF and are byte-identical when off; the trajectory `schema_version` is `0.13.0`.

## 0.1.0

Initial scaffold: agent loop, six tools (exact-match edit), LiteLLM gateway, schema-versioned trajectory
capture, mandated verification, eval harness. Standalone CLI.
