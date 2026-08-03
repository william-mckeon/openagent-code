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

## Features (specs/0022–0057)

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
