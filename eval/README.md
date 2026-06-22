# eval — the steering wheel

The eval harness is how you turn "it worked once" into a **number**: the pass
rate across a held-out set of tasks. That number is what tells you whether a new
model — or a harness change — is actually better. Without it, the training
flywheel is flying blind.

## Run it

```powershell
python -m eval.harness
```

It runs against whatever `.env` points at (`CODE_MODEL`, `CODE_API_BASE`, …) in
the current `CODE_TOOL_MODE`. Each task:

1. gets a fresh sandbox repo (the `setup:` files),
2. is handed to the agent (`build_agent`, same as the CLI),
3. is checked by an objective `verify:` command (exit 0 == pass).

Output is per-task plus a final pass rate:

```
[PASS] fix_subtraction_bug.yaml   outcome=success      tool_calls=5
[FAIL] grep_and_fix.yaml          outcome=verify_failed tool_calls=8
...
4/5 passed (80%)
```

`outcome` uses the same honest labels as the CLI, so a real failure
(`verify_failed`) is distinguished from the agent doing nothing (`no_action`,
`protocol_stalled`).

## Trajectories are kept

Unlike a throwaway run, the eval **persists** each trajectory to
`trajectories/eval/` (the sandbox code dir is discarded; the captured trajectory
is kept). So an eval run doubles as your first real batch of training data —
varied, labelled, with the verification reward attached.

## Tasks

Tasks live in `eval/tasks/*.yaml`:

```yaml
prompt:  what to ask the agent
setup:   { path: content, ... }   # files written into the sandbox first
verify:  shell command            # exit 0 == passed
```

Keep `verify` dependency-light (plain `python test_x.py` with asserts) so eval
runs anywhere without pytest. Add tasks freely — the more varied the set, the
more trustworthy the number. `indentation_edit.yaml` is a deliberate regression
test for the edit-indentation guard; `grep_and_fix.yaml` forces real search.

## Agentic (behavior) tasks

`eval/agentic/*.yaml` score *how the agent behaved*, not just whether code ended
up correct — the quality the verify eval is blind to (did it read enough, refuse,
finish, catch the real issues?). No `verify:`; instead a `rubric:` is scored by
`rubric.py` over the trajectory:

```yaml
prompt: ...
kind: review
rubric:
  min_files_read: 3          # depth: opened at least N files
  no_refusal: true           # didn't punt with "narrow the scope"
  expect_final: true         # produced a real final answer
  must_mention:              # FINDINGS: the answer must name these (case-insensitive)
    - ["hardcoded", "secret in code"]   # a list entry = any synonym satisfies it
    - "sql injection"
```

`must_mention` is the discriminating check (Stage 3): reading the files isn't
enough — a shallow review that misses the planted issue scores lower than one that
catches it. The run prints `unmentioned=...` for whatever was missed.

## Tiers and the gate

Every task may set `tier:` — `smoke` (trivial), `core` (default), or `hard`
(meant to have headroom even for a strong model). Run one tier with
`--tier hard`. The summary breaks the pass rate down per tier and prints a **gate
verdict**:

```
[gate] suite reads 100% — it cannot yet discriminate. Add harder tasks ...
[gate] suite discriminates (something scored below 100%) — usable as a promotion gate.
```

This matters for the distillation flywheel (`specs/0005`): a promotion gate that
always reads 100% can't tell a good student from a bad one. The `hard` tier
exists to keep real headroom in the number.
