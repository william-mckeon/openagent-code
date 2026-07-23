"""
src/fanout.py

Bounded parallel fan-out (Workflows P2 / specs/0039).

ONE shared helper for the three fan-out sites (workflow.run_workflow, orchestrator.review_repo,
skills.run_skill): run `spawn(task)` over `tasks` and return the results POSITIONALLY ALIGNED to `tasks` in
SUBMISSION ORDER, so the reduce/digest is deterministic regardless of completion order.

Stdlib-only (concurrent.futures) — no config/model/litellm import — so the dep-free harness can drive it
with a fake spawn and prove the concurrency semantics with zero model calls.

Byte-identity: at max_workers <= 1 (the default) it is literally [spawn(t) for t in tasks] — no executor is
constructed and read_only is never set — so a flag-default run is identical to the serial loops it replaces.
"""
from concurrent.futures import ThreadPoolExecutor


def fanout(spawn, tasks, max_workers):
    """Map `spawn` over `tasks`, returning a list aligned to `tasks` in submission order.

    max_workers <= 1 (or <= 1 task): serial fast path, `[spawn(t) for t in tasks]` — no executor, no
    read_only, byte-identical to a plain loop. Above 1: up to max_workers run concurrently on a bounded
    ThreadPoolExecutor; because parallel children could otherwise race the filesystem, each is spawned
    READ-ONLY (`spawn(task, read_only=True)`). Results are gathered in submission order; a task that raises
    re-surfaces at its own slot while already-submitted siblings still run to completion (the executor's
    shutdown waits for them)."""
    tasks = list(tasks)
    if max_workers <= 1 or len(tasks) <= 1:
        return [spawn(t) for t in tasks]
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = [ex.submit(spawn, t, read_only=True) for t in tasks]
        return [f.result() for f in futures]
