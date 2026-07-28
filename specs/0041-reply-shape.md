# 0041 — Honor the user's reply shape; fix spaced-path grants

Status: accepted
From a live Arcus session review (the operator told it to "respond with only Yes" and it dumped a full
repo review instead; later it answered real questions with a stale "Yes"/"No."). Two fixes:

- **F1 (flag-gated, `CODE_REPLY_SHAPE`, default off):** teach the harness that an explicit user reply
  *shape/length* instruction outranks a tool's "synthesize now" trailer, and that such an instruction is
  **per-turn** (does not carry to later turns).
- **F3 (bug fix, within `CODE_TRUST_USER_DIRS`):** a user-typed path with a **space** in it
  (`…\resume helper`) was truncated at the space and the wrong sibling (`…\resume`) was auto-granted.

## F1 — reply-shape precedence (`CODE_REPLY_SHAPE`)

The `review_repo`/`run_workflow`/`run_skill` digests end with a hard imperative trailer — *"Write the FINAL
review NOW … your next reply must be the finished review"* (orchestrator.py, workflow.py, skills.py). Nothing
in `BASE_PROMPT` said a user's explicit reply-shape instruction outranks that trailer, and nothing scoped a
format instruction to the turn it was given on. Result: the trailer beat "only Yes" (and a stale "only Yes"
later beat a real question). When `CODE_REPLY_SHAPE` is on:

- **`prompts.build_system_prompt`** appends one counterweight paragraph: a user shape/length instruction for
  THIS turn outranks any tool "write the full report / synthesize now" trailer — give exactly what was
  asked; and such an instruction applies ONLY to its turn, so a later open-ended question gets a full,
  normal answer.
- **`context.set_task`** pins the request with precedence language ("the ONLY instruction in force this
  turn; an earlier turn's format/length constraint does not apply unless repeated here") instead of the
  neutral "answer THIS directly".
- **The three digest trailers** append `prompts.reply_shape_caveat()` — *"unless the user constrained your
  reply this turn; if so give exactly that and hold this synthesis"* — so the "synthesize now" command
  yields to an explicit shorter ask.

**Honest scope (from the diagnosis, verified):** this is a *partial* mitigation, not a cure. The dump's
shape is a verbatim fingerprint of the trailer, but Arcus had already *chosen to run `review_repo`* before
any trailer existed — a model honoring "only Yes" would not have fanned out. Softening the trailer + a
precedence rule is a prompt/pin nudge on a weak model (gpt-oss-120b); the durable fix for the tool-selection
and the terse-anchoring (F2) is the flywheel. This flag makes the harness pull the right way.

## F3 — spaced-path grants (`src/userdirs.py`)

`_PATH_TOKEN`'s tail class stops at whitespace, so `user_typed_dirs` only saw `…\resume` and granted that
existing sibling. Fix, keeping the denylist + read-only tier:

- **Quoted span:** if the user quoted the path (`"C:\a\b c"`), extract the whole quoted content.
- **Progressive-longest:** for an unquoted anchored token, extend it word-by-word over following spaces and
  grant the **longest** candidate that `os.path.isdir` **and** `grantable_dir` accepts. `…\resume helper`
  (longer) wins over `…\resume`. A no-space path yields a single candidate → **byte-identical** to today.

## Acceptance

`scripts/check_reply_shape.py` (dep-free): with `CODE_REPLY_SHAPE` off, `build_system_prompt`, the
`set_task` pin, and `reply_shape_caveat()` / the three trailers are byte-identical to today; with it on, each
carries the precedence text / caveat. `scripts/check_trust_dirs.py` gains: a spaced path grants the full
`…\a b` (not `…\a`), a no-space path is unchanged, a quoted spaced path is extracted, and the denylist still
blocks a spaced system path.

## Byte-identity

`CODE_REPLY_SHAPE` off → no prompt paragraph, neutral pin, empty caveat → every prompt/pin/digest is
unchanged. The F3 fix only changes output when `CODE_TRUST_USER_DIRS` is on **and** the typed path contains a
space. No `safety_fingerprint` change, no `SCHEMA_VERSION` bump. `docker/code/Dockerfile` is a deliberate
no-op (both are opt-in / a bug fix inside an opt-in feature).
