# 0083 — OS sandbox (restricted-token + job-object spawn, fail-closed auto-approve)

Status: implemented (restricted-token confinement VALIDATED on a Windows host — see Acceptance)
Flags (all default OFF): `CODE_SANDBOX_SPAWN`, `CODE_SANDBOX_REQUIRED`, `CODE_REQUIRE_SANDBOX_FOR_AUTO`,
`CODE_SANDBOX_JOB_MEM_MB` (0=off), `CODE_SANDBOX_JOB_MAX_PROCS` (0=off), `CODE_SANDBOX_WRITE_RESTRICTED`

## Goal

Phase 3 of the Codex-vs-OAC security adoption — the OS-sandbox cluster. Every other OAC control is in-process
or advisory (execpolicy, the FS fence, the guardian): a determined command that reaches the shell runs with the
agent's FULL authority. This adds the one real KERNEL boundary OAC can have on Windows without a third-party
dependency — spawn `run_command` children with LESS authority than the agent, and make auto-approval contingent
on that boundary actually being in force.

- **#2 restricted token.** `CreateRestrictedToken(DISABLE_MAX_PRIVILEGE | LUA_TOKEN [| WRITE_RESTRICTED])`
  derives a *lesser* version of the process's OWN token — every privilege dropped, powerful groups disabled,
  de-elevated to Medium integrity. Because it's a restricted derivative of the caller's token (not a different
  user), `CreateProcessAsUserW` assigns it with NO `SE_ASSIGNPRIMARYTOKEN` privilege — the standard
  Chromium-sandbox technique.
- **#14 job object.** The child spawns into a `CreateJobObject` with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`
  (+ optional per-process memory / active-process caps). Closing the job handle (or the agent dying) kills the
  whole child TREE — no orphaned runaway escapes the turn — and a fork/memory bomb is capped.
- **#4 fail-closed when required.** `CODE_SANDBOX_REQUIRED`: if a command WOULD be sandboxed but the sandbox is
  unavailable on this host, REFUSE to run it rather than run unconfined.
- **#3 auto-approve requires the sandbox.** `CODE_REQUIRE_SANDBOX_FOR_AUTO`: a mutating `run_command` is
  auto-ALLOWED only if it will actually be sandboxed; otherwise the allow is downgraded to ask (guardian, then
  human). Read-only commands are unaffected (safe unconfined).

## Concepts

- **`src/winsandbox.py`** (new, dep-free, ctypes). Pure helpers `restricted_token_flags` / `job_limit_flags`
  are import-safe and unit-tested. `available()` does a REAL, cached PROBE SPAWN — it returns True only if a
  restricted-token child actually ran, so it NEVER claims confinement it can't deliver. `run_shell()` spawns
  under the restricted token + job (assign-while-suspended → resume, so confinement precedes the first
  instruction), captures MERGED stdout+stderr via an inherited pipe drained on a thread, enforces the timeout
  (terminate + job-close reaps the tree), and raises `SandboxUnavailable` rather than ever running unconfined.
  All Win32 calls have explicit `argtypes`/`restype` (essential on 64-bit — otherwise ctypes truncates HANDLEs
  and overflows on the -1 pseudo-handle).
- **`tools._sandboxed_run`** routes `run_command` through the sandbox when `CODE_SANDBOX_SPAWN` is on; returns
  a ToolResult when it handled the command (ran / refused / timed out) or None to fall back. FAIL-CLOSED:
  unavailable + `CODE_SANDBOX_REQUIRED` → refuse; unavailable + not required → run unconfined but ONLY with a
  logged warning (never a silent pretend-confine).
- **`permissions._sandbox_gate`** implements #3 on the two MUTATING auto-allow paths (an allow-rule match and
  bypass mode).

## Acceptance

`scripts/check_winsandbox_0083.py` (12/12, dep-free). The fail-closed / gating logic is proved by forcing
`winsandbox._AVAIL`, so it verifies on any host: #4 refuses (and the command leaves no side effect on disk); the
not-required fallback runs unconfined; #3 downgrades an auto-allow when not sandboxed and keeps it when
sandboxed; all three are byte-identical with the flags off. **On this Windows host the live path is real**: a
restricted-token child spawns and its output is captured, and a manual probe confirmed the child's privileges
are stripped **5 → 1** (only the unremovable `SeChangeNotifyPrivilege` remains) at Medium integrity — a genuine
authority reduction, not a paper one. Full dep-free suite green.

## Non-goals / honest scope

- **`WRITE_RESTRICTED` is opt-in and default-off.** Full filesystem write-path confinement needs the workspace
  granted to a restricting SID (or the child's writes fail wholesale); shipping it on by default would break
  normal builds. The default token drops privileges + de-elevates (validated), which is the high-value,
  low-breakage core; write-SID confinement is a stricter follow-on for an operator who sets up the ACL.
- **Availability is host-dependent, by design.** Where the token spawn lacks the needed privilege (locked-down
  service accounts, some CI), `available()` is False and the operator chooses refuse (`SANDBOX_REQUIRED`) or
  unconfined-with-warning. The probe guarantees we never *claim* confinement we didn't get.
- **Output is stdout+stderr MERGED** on the sandbox path (a single pipe keeps the ctypes spawn robust); the
  non-sandbox path still separates them. The sandbox path is opt-in, so this is an accepted presentation
  difference, not a regression.
- Network egress confinement (#5b / `0018`) is still out — it needs WFP/AppContainer and its own timeline.

## Byte-identity

Every flag defaults OFF: `_sandboxed_run` and `_sandbox_gate` return immediately (None), so `run_command` and
the permission ladder are byte-identical to pre-0083. Verified: full dep-free suite green with the flags off.
