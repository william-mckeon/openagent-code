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
