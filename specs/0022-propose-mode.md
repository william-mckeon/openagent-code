# 0022 — propose mode (a change manifest the user approves before any edit)

## Goal
Make openagent-code a real team member: for a substantive change it should PROPOSE a structured
change-list — the files it will add / move / update / delete, each with a one-line why — and let the user
approve the whole plan ONCE, then execute exactly that plan. This is how a careful reviewer hands over
"here's what I'm about to do" before touching anything.

Two shapes, one machinery:
- **`--mode propose`** makes it mandatory: the agent INVESTIGATES read-only, calls `propose_changes`,
  waits for approval, and only then may edit. Selecting the mode turns the feature on (`config.PROPOSE`).
- **In any other mode** (default / acceptEdits / bypass) the same tool is offered when `CODE_PROPOSE=true`,
  and the agent ELECTS it for a broad or destructive change — proposing the list and asking a single
  `[y/N]` before editing. A one- or two-line edit just proceeds (no friction).

Approving the whole list up front means execution never has to ask per file: you consent at the PLAN
level. Hooks / the guardian become the NET for OFF-PLAN deviations, not gatekeepers on the approved plan.

## The one thing mode alone can't express
`propose` must be READ-ONLY during investigate and ALLOW the approved edits during execute — the same mode,
two behaviors. So the phase is a per-ctx flag, `ctx.propose_phase` (`investigate` -> `approved`), that the
approval flow flips and the permission engine READS. It defaults to `investigate` in propose mode and is
reset every task, so entering the mode is read-only until an explicit approval, and an approval never leaks
to the next turn.

## The graduated off-plan net (ask, don't hard-deny)
Once a manifest is approved, an op that is OFF it is graded, not blindly allowed:
- low-risk (a plain edit/write) -> allow + log,
- high-risk (`_is_destructive`: delete / move / dangerous command) -> ASK (guardian headless / prompt
  interactive / block), bounded by `CODE_GUARDIAN_MAX_DESTRUCTIVE` for free.

The same escalation delivers the deferred spec-0015 non-goal: a PreToolUse hook may now return
`{"decision": "ask"}`, and the engine escalates it to the ask ladder instead of a hard deny. This is an
ALLOW-post-process: it can only ever DOWNGRADE an allow to an ask, never upgrade a block. The hard
guarantees (deny rules + fence) still run FIRST and win — an approved manifest can never pre-approve an
edit to `.env` or a path outside the workspace.

## Acceptance
- `src/config.py`: `PROPOSE` (`CODE_PROPOSE`, default false) + `"propose"` in `_MODES`.
- `src/cli.py`: `"propose"` in the REPL `_MODES`; `main()` turns `config.PROPOSE` on when the resolved mode
  is `propose` (so `--mode propose` / `CODE_PERMISSION_MODE=propose` is never a dead read-only mode).
- `src/tools.py`: `propose_changes` (registration-only: validates the manifest, stashes `ctx.manifest`,
  collects ONE plan-level `[y/N]` via `ctx.ask`, flips `ctx.propose_phase='approved'` + fills
  `ctx.approved_paths`; top-level only; headless -> write the plan to `.openagent/` and STOP, never
  auto-approve) + `PROPOSE_TOOLS`; three new `Context` fields (`manifest`, `propose_phase`,
  `approved_paths`).
- `src/toolset.py`: offers `PROPOSE_TOOLS` only when `config.PROPOSE` (the ONE gate — not the mode, so
  auto-propose works in the other modes too).
- `src/permissions.py`: an approved-manifest ALLOW + a propose-investigate DENY, mirrored across ALL THREE
  mutation ladders (`decide()`, `_decide_command()` under EXECPOLICY, `decide_move()`); the hook-ask +
  off-plan escalation as an allow-post-process (`_escalate`), UNDER deny + fence.
- `src/hooks.py`: `pretool()` surfaces an `ask` verdict (deny still wins across hooks).
- `src/agent.py`: per-task reset of `ctx.manifest / propose_phase / approved_paths`; log the manifest ONCE
  at resolution (`_finish`).
- `src/prompts.py`: a propose-protocol note, gated on the tool's PRESENCE (not a mode string).
- `src/outcomes.py`: `"manifest_declined"` in `GATE_OUTCOMES` (auto-propagates to `subagent._classify` +
  `eval/rubric`).
- `src/trajectory.py`: `log_manifest` + `SCHEMA_VERSION` 0.9.0 -> 0.10.0.
- `train/convert.py`: `_unapplied_manifest_turns` (drop a declined turn, keep the good ones beside it) +
  a one-shot `manifest_declined` guard + a report counter.
- `scripts/check_propose.py` — dep-free, no model / network.
- **Flag OFF is byte-identical**: `propose_changes` isn't offered, every new decide()/hooks branch is
  guarded by `config.PROPOSE` (or a non-None hook-ask), and no record shape changes on a flag-off run.

## Traps (each is a test)
- **Phase, not mode** — the SAME `propose` mode denies during investigate and allows during execute; the
  `ctx.propose_phase` flag is mandatory and `getattr(ctx, "propose_phase", "investigate")` must agree with
  its default.
- **Three mutation ladders** — `decide()`, `_decide_command()` (run_command under EXECPOLICY, diverted
  BEFORE the mode branch), and `decide_move()` each need the propose branch, or a mutating command / move
  slips through during investigate.
- **Two `_MODES` sets** — `config._MODES` (env path) and `cli._MODES` (REPL `/mode`) must both gain
  `propose`.
- **`propose_changes` is NOT mutating** — it only records intent (like `update_plan`), so it must stay out
  of `permissions.MUTATING` or it would be blocked in plan / headless and could never be proposed.
- **Approval is UNDER the hard rules** — deny rules + fence precede the approved-manifest allow; an
  approved op targeting `.env` or outside the fence is still blocked.
- **Escalation only downgrades** — the hook-ask / off-plan escalation runs only on an `allow` result, so it
  can never upgrade a deny/block to a maybe; deny wins over ask ACROSS hooks (remember the ask, keep
  scanning for a later deny).
- **Path normalization** — `ctx.approved_paths` is keyed the SAME way `decide()` keys a target
  (workspace-rel, forward-slashed, `os.path.normcase`), and a move carries BOTH endpoints, or an approved
  edit reads as off-plan.
- **Headless never auto-approves** — no human -> write the plan out and STOP; an EOF / empty answer is a
  decline, not an approval.
- **Cross-turn leak** — reset `ctx.manifest / approved_paths / propose_phase` every task (the same leak
  class the plan/goal resets fix), or a plan approved for one task authorizes edits on the next. This is the
  DEFAULT and the guarantee for file writes. specs/0048 adds three OPT-IN, default-off relaxations for
  graduated follow-through (run/test after approval, prompted extension, and a scoped-bypass persist) — see
  that spec; with all off, this Trap holds byte-for-byte and `approved_paths` is still reset every turn.
- **A declined plan is not a keeper** — a manifest with `approved != True` drops that turn from SFT via
  `_unapplied_manifest_turns` (NOT `_contested_turns`: a decline writes no permission record).

## Non-goals (v1)
- Executing the manifest with a dedicated executor tool — the agent emits ordinary edits that re-enter the
  gate per file (the approved-manifest allow passes them), so `apply_patch` and the gates cover it for free.
- Feeding the approved manifest to the guardian so it can judge "deviates from the plan" (the guardian
  still judges each op against the request; plan-aware review is a later phase).
- An off-plan net for a `Move` in a permissive mode (a rename is recoverable; it follows the existing
  `decide_move` baseline).
