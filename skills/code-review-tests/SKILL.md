---
name: code-review-tests
description: Test coverage for the behavior the change adds or modifies.
---

Check whether the changed logic is covered. Does new or modified behavior have a matching test —
a pytest under the repo's test dir, or (for agent behavior) an eval task / a verify command? Flag:
changed logic that is left untested, tests that assert nothing meaningful, and cases where an
existing test helper should have been reused instead of new scaffolding. For each gap, name the
file:line of the changed behavior and the specific test that is missing. Don't demand tests for
statically-defined values or pure renames.
