# Specs — spec-driven development

Specs are the **source of truth** and double as the agent's definition of "done"
(i.e. a reward function written in English). Workflow:

1. Write a spec here, e.g. `specs/0001-feature-x.md`.
2. Ask the agent to implement it:
   `python -m opencode "Implement specs/0001-feature-x.md. Verify against its acceptance criteria."`
3. The agent reads the spec, implements, and runs the acceptance check.
4. Reconcile any drift back into the spec so it stays accurate.

Suggested spec shape (adapt the GitHub Spec Kit conventions):

```
# <feature name>
## Goal            — one paragraph, why this exists
## Acceptance      — checkable bullet list; ideally each maps to a test
## Non-goals       — what this explicitly does NOT do
## Notes           — constraints, edge cases
```

Because acceptance criteria are checkable, every spec-driven session yields a
high-quality outcome label for the trajectory in `trajectories/`.
