---
name: code-review-correctness
description: Logic bugs, edge cases, and error handling introduced by the change.
---

Look for correctness defects the diff INTRODUCES: logic errors, off-by-one and boundary mistakes,
unhandled error paths, swallowed exceptions, wrong `ok=False`/return contracts, None/empty
mishandling, and behavior that contradicts the surrounding code or the change's stated intent.
Trace the changed lines and how they're called. Flag only real defects — for each, give the
file:line, a one-line description of the wrong behavior, and the concrete input/state that triggers
it. Do not restate what the code does; only flag what's wrong.
