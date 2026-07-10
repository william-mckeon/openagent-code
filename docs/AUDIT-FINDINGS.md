# Adversarial audit — findings ledger

A full-phase adversarial audit (2026-07, 15 review dimensions across all 14 phases + the cross-cutting
seams, every finding refuted-by-default by an independent verifier) surfaced **19 confirmed latent bugs**
while all 14 unit harnesses were green — because they all live in a **seam** (between turns, between
phases, across a non-standard config) that isolated, mocked, single-run unit checks structurally can't
see. Every one is now fixed **and pinned by a seam-level check** so it can't regress.

Integration testing *discovers* these; a deterministic `check_*.py` *pins* them. That ratchet is the
rule going forward (see the deviation matrix in [VALIDATION.md](VALIDATION.md)).

## Status: 19/19 fixed

| # | Sev | Finding | Fix | Commit |
|---|-----|---------|-----|--------|
| 1 | 🔴 crit | `apply_patch` bypassed the fence, deny/ask rules & plan mode (write/delete anywhere on disk) | mutating tool + per-op `decide()` in `patch.py` | `eaf63a1` |
| 2 | 🟠 high | REPL blanket-labeled a session `completed`, keeping degenerate/ungrounded/unverified turns as SFT rows | per-turn `turn_outcome` (0.7.0) + `convert` filters per turn | `7111c84` |
| 3 | 🟠 high | `eval/harness.run_task` collapsed honest labels to `success` on the corpus path | one shared `outcomes.classify` | `7111c84` |
| 4 | 🟠 high | `convert` dropped the reasoning channel from SFT targets (trained reasoning-free tool calls) | fold reasoning into the tool-call target, mirror the planner | `d967864` |
| 5 | 🟠 high | `rollback` used an absolute index `_compact` invalidates → dangling tool_call poisons the next turn | snapshot the working set, not its length | `fb0720e` |
| 6 | 🟡 med | `ctx.spawn_count` never reset → fan-out cap bled across turns, blocking `spawn_agent` | added to the per-task reset | `0231e92` |
| 7 | 🟡 med | `is_trainable` all-or-nothing dropped good turns for one late failing turn | per-turn verify scope | `7111c84` |
| 8 | 🟡 med | completion-gate ledger match case-sensitive on Windows → spurious "not backed" | `os.path.normcase` lookup | `0231e92` |
| 9 | 🟡 med | Tier-2 grounding skipped a prose-only absence claim ("auth has no Go source" — src/auth has 14 .go) | spawn on cited-path **or** absence claim | `8926a99` |
| 10 | 🟡 med | `edit_file`/`write_file` rewrote whole LF files to CRLF on Windows | verbatim write + detect-and-restore | `0231e92` |
| 11 | 🟡 med | `apply_patch` not atomic in the apply phase (partial writes, ledger blind) | transactional apply with rollback | `9948fb6` |
| 12 | 🟡 med | `verify_edits.verifier_cmds()` raised `AttributeError` on a valid-JSON non-dict config | validate dict → fail open | `9948fb6` |
| 13 | 🟡 med | `looks_degenerate` false-flagged scattered identical lines | require a consecutive run | `6201d1c` |
| 14 | 🟡 med | reasoning-leak detector missed "thus the final answer:" | `_CONCLUSION_META` + anchored strip | `6201d1c` |
| 15 | 🟡 med | `tree` was not fence-classified (enumerated outside the workspace) | path-tool in `_target` | `eaf63a1` |
| 16 | ⚪ low | grounding deterministic tier case-sensitive on Windows | `normcase` set | `8926a99` |
| 17 | ⚪ low | `_safe_cut` IndexError at `COMPACT_KEEP_RECENT=0` | index guard | `fb0720e` |
| 18 | ⚪ low | `apply_patch` apply loop caught only `OSError` (NUL/surrogate escaped) | catch `OSError/ValueError/UnicodeError` | `9948fb6` |
| 19 | ⚪ low | `looks_degenerate` missed a ticking-counter loop | digit-normalize each line | `6201d1c` |

## What the fixes cost / added
15 dep-free harnesses, **246 checks total** (~+40 this pass), all green. New seam-level coverage:
`decide('apply_patch')`/`decide('tree')`, the fence enforced inside `patch.py`, per-turn `convert`
filtering, compaction-invariant rollback, the case-fold/CRLF/spawn_count leaks, the prose-only absence
claim, transactional apply rollback, and the reasoning-fold target.
