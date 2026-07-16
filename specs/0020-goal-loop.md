# 0020 — goal loop (pursue a machine-checkable bar, unattended)

## Goal
Stop being the loop. Today the human is the control flow: prompt → agent edits → human runs the test →
human pastes the error → repeat. This phase moves that loop INTO the harness: the agent recognizes a task
with a **verifiable end state** ("make the tests pass"), declares the bar, and the harness iterates until
the bar passes or a budget runs out.

**The model proposes; the harness disposes.** The agent calls `pursue(objective, bar, max_iterations)`;
from then on the HARNESS owns control flow. The model NEVER decides "done" — **the bar command does**.
This is the same split the codebase already uses: `review_repo` (the model may propose `areas`, the
harness does the deterministic fan-out + `_balance_plan`), the gates (the model works, the harness owns
the bounded retry + honest outcome), the guardian (the model judges, the deterministic cap is the ceiling).

**The entry filter IS the loop-shape heuristic.** A runnable bar is required; no bar → no loop. "Make the
tests pass" → `["npm","test"]` → loops. "Refactor this nicely" → no checkable bar → just do the work. We
never need a fragile "is this loop-shaped?" classifier.

## The trust inversion (the load-bearing difference from 0014)
`verify_edits` (0014) takes **ARGV lists from an OPERATOR-configured file**, run `shell=False` — its
docstring says *"there is no shell to inject."* The goal bar is **MODEL-proposed** and re-run every
iteration, unattended. That inverts the trust, so the bar is defended in **four** layers:

1. **Shape** — `pursue` REFUSES anything but a non-empty **argv list**. A shell string is rejected at the
   tool boundary (a model-proposed shell string would hand `rm -rf /` a runner).
2. **Entry filter** (`goal.entry_ok`) — reject a bar that execpolicy classifies **DANGEROUS**; reject a
   shell/interpreter `argv[0]` (`sh`, `bash`, `zsh`, `cmd`, `powershell`, `python -c`, …) which would
   re-open the shell we just closed; reject anything outside `CODE_GOAL_BARS_CONFIG` when an operator
   allowlist is configured.
3. **Permission gate** (`goal.gate`) — the bar is gated through `permissions.decide("run_command", …)`
   ONCE at loop entry, so deny rules / fence / execpolicy / guardian / hooks all apply to it.
4. **Execution** — argv, `shell=False`, cwd-bound, timeout.

## Acceptance
- `src/goal.py`: `normalize_bar` / `render` / `entry_ok(bar) -> (ok, why)` / `gate(bar, ctx)` /
  `run_bar(bar, cwd, run_fn=None) -> (ok, output)` / `challenge(objective, output)`. Pure + injectable
  `run_fn`, imports only config/logsetup/execpolicy, never raises.
- `src/tools.py`: `pursue(args, ctx)` — depth-0 only (a child must not pursue), argv-only, entry_ok +
  gate, stashes `ctx.goal`. `GOAL_TOOLS` exported like `PATCH_TOOLS`.
- `src/toolset.py`: `active_tools()` adds `GOAL_TOOLS` only when `CODE_GOAL_LOOP` — **the only offering
  site**, so a flag-off run logs the identical `tool_schemas` it logs today.
- `src/permissions.py`: `"pursue"` in `MUTATING` (else `decide()` auto-allows it as a read-only tool and
  the bar faces NO gate) + a `_target` case rendering the bar (else the guardian reviews a blind
  `'pursue'` — the `apply_patch`-hides-its-target bug, again).
- `src/agent.py`: the **goal gate** in the `if not decision.calls:` chain, after completion/auto-verify and
  BEFORE grounding (grounding must judge the real final answer). `ctx.goal` reset per task. The gate
  self-terminates near the step ceiling.
- Honest termination: `goal_unmet` in `outcomes.GATE_OUTCOMES` **and** `subagent._classify` **and**
  `eval/rubric.verified_done` — three sites, not one.
- `scripts/check_goal.py`: dep-free, no model/network (injected `run_fn`), proving every trap below.
- **Flag OFF (default) is byte-identical**: the tool isn't offered, the gate is a no-op.

## Traps (each is an acceptance test, not a footnote)
- **The destructive cap does NOT bound bar repetition.** `_ask_approver` counts DISTINCT targets, so a bar
  gated once consumes 1 of `GUARDIAN_MAX_DESTRUCTIVE` and then executes N times without re-entering
  `decide()`. The cap bounds **breadth, not repetition** — so the bar gets its OWN per-loop ceiling
  (`max_iterations`, capped by `CODE_GOAL_MAX_ITERATIONS`), and a DANGEROUS bar is refused outright.
- **`max_steps` fall-through.** When steps run out mid-loop, `run()` falls past the gate chain to the
  SYNTHESIS_PROMPT path and returns `max_steps`, so `goal_unmet` is unreachable on the common path. The
  gate must refuse to re-prompt within `CODE_GOAL_STEP_HEADROOM` of the ceiling and return `goal_unmet`.
- **The completion gate can preempt the loop.** `verify_retries` is a per-RUN counter (cap 2) spent
  GLOBALLY across iterations; once exhausted it returns `unverified_completion` and escapes. Both labels
  are honest and non-trainable, so the corpus stays safe — but the bar does not own termination
  absolutely, and the spec says so rather than pretending otherwise.
- **Log ONLY the final bar result.** `convert.trainable_turns` does `verif_ok = verif_ok and r["ok"]`, so
  logging each failing iteration would drop every SUCCESSFULLY CONVERGED loop from the corpus. 0014
  already solved this ("record the FINAL result … NOT the intermediate attempts").
- **Three classify sites.** `subagent.py` keeps a PRIVATE `_classify` (AUDIT-FINDINGS row 3 migrated
  `eval/harness.py` and left this one). Miss it and a subagent whose loop thrashed returns `completed` →
  a keeper outcome → the thrashing loop becomes a positive SFT target.
- **Don't put the iteration counter on `ctx`** unless it's in the per-task reset — that's the
  cross-turn-leak class (`spawn_count` is the precedent that this gets forgotten).
- **`CODE_AUTO_APPROVE=true` (bypass) makes the destructive cap inert** — never ship the loop ON in
  `.env.example`.

## Non-goals (v1)
- A cron/scheduled loop (that's a shell script around the one-shot CLI).
- A model-JUDGED bar ("is it good?") — the entry filter exists precisely to forbid it.
- Nested pursuit (a child pursuing its own goal) — depth-0 only.
- Bars that need a shell (pipes, redirects, globs). Argv only, by construction.
