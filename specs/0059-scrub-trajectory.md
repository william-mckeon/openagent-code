# 0059 — trajectory PII / secret scrubbing

Status: implemented
Flag: `CODE_SCRUB_TRAJECTORY` (default off)

## Goal

Keep secrets and personal data out of the training corpus. The trajectory captures everything the agent
reads and writes verbatim — so the moment the user pasted their live EveryDollar budget (paychecks,
mortgage, debt balances) and the authenticated page HTML (email, userId, a live `_CSRF` token), all of it
flowed straight into the flywheel's JSONL. It had to be purged by hand. For a self-hosted, data-sovereign
finance agent being fed real data, the capture must scrub itself.

## Concepts

- **Scrub at the single write choke point.** `src/scrub.py` provides `scrub_text(str)` and `scrub_record(obj)`
  (recurse dict/list, scrub string VALUES, leave keys and non-strings). `trajectory._write` — the one method
  all 14 `log_*` records pass through — runs `scrub_record` on the record when `CODE_SCRUB_TRAJECTORY` is on,
  BEFORE `json.dumps`. One hook covers every record type.
- **Persisted copy only; the live agent is unaffected.** `scrub_record` returns a COPY — the in-memory
  record (and the live context the agent works from this turn) is never mutated, so the agent still builds
  the app from the real budget while the flywheel only ever sees the redacted version.
- **Two tiers.** SECRETS (high confidence, near-zero false positives): private-key blocks, JWTs, provider API
  keys (`tgp_v1_`, `sk-`, `ABSK`, `AKIA`, `tvly-`, `ghp_`, `xox*-`, `AIza…`), bearer tokens, name-anchored
  token/secret/password assignments (CSRF/session/access/refresh/api-key), and emails. FINANCIAL PII:
  currency-formatted amounts (`$1,234.56`, cents-anchored so a bare `$5` or `version 1.2` is safe),
  user/account/customer-id assignments, 16-digit card numbers, and SSNs.
- **Visible markers.** A match becomes `[redacted:<kind>]` (email / token / amount / id / card / ssn /
  private-key / jwt), so the trajectory stays valid JSON and a reader can see WHAT was removed, never the
  value. The marker carries no secret/label, so re-scrubbing on a resume/re-write is idempotent.
- **Fail-safe.** A pattern that raised is skipped, never dropping the record. Dependency-free (`re` only).

## Acceptance

Each item is an assertion in `scripts/check_scrub.py` (23/23, dep-free):

- Every secret class from the live run is redacted (value gone, marker present): the email, the `_CSRF`
  assignment (name kept), `tgp_v1_`/`sk-`/`AKIA`/`ghp_` keys, a bearer token, a private-key block.
- Financial PII redacted: the budget's dollar amounts (labels kept), a `userId` assignment, a card number.
- Clean content is UNTOUCHED (no over-scrub): code, paths, `docker-compose up`, `go 1.22`, "responds 200 on
  localhost:8080", a tool-call log line.
- `scrub_record` recurses, redacts values but keeps keys + non-string scalars, and returns a COPY (original
  unmutated); scrubbing is idempotent.
- `trajectory._write` integration: with the flag ON the persisted `.jsonl` has no email/amounts (redacted);
  with it OFF the file is verbatim (byte-identical).
- `CODE_SCRUB_TRAJECTORY` defaults False when unset.

## Non-goals

- Not a scrub of the LIVE context / memory / model prompt — the agent needs the real data to work this turn;
  only the persisted trajectory is scrubbed. (Redacting the prompt is a separate, larger concern.)
- Not a guarantee against every possible secret shape — it targets the known high-value classes; err toward
  catching (financial amounts accept some false positives, per the chosen scope).
- No `SCHEMA_VERSION` bump (record SHAPES are unchanged; only string values within them differ), and not added
  to `safety_fingerprint`.

## Byte-identity

`CODE_SCRUB_TRAJECTORY` off (default): `_write` never imports or calls `scrub`, so the JSONL is byte-for-byte
what it was. Verified: the flag-off trajectory assertion in `check_scrub.py`, and the full suite unchanged
(51/51) — every other harness that reads/writes trajectories is unaffected.
