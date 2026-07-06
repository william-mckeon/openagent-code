---
name: code-review-breaking-changes
description: Changes to external surfaces that could break callers or the captured corpus.
---

Look for changes to this project's EXTERNAL surfaces that could break users or the flywheel:
- tool JSON schemas in `src/tools.py` (a renamed/removed/newly-required parameter),
- `CODE_*` env-var names or defaults in `src/config.py` (and their `.env.example` docs),
- permission-rule matchers in `permissions.json`,
- the trajectory / JSONL shape the converter reads (a `schema_version` bump, renamed record fields),
- CLI flags / commands in `src/cli.py`.
For each, give the file:line, what breaks, and who depends on it. A silent behavior change to a
surface something else relies on counts even if nothing errors at the call site.
