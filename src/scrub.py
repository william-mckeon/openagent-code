"""
src/scrub.py

Trajectory PII / secret scrubbing (Phase 59 / specs/0059).

When CODE_SCRUB_TRAJECTORY is on, every trajectory record is passed through scrub_record() at the SINGLE
write choke point (trajectory._write) BEFORE it is serialized to JSONL — so a pasted secret, session token,
email, or budget/statement never enters the training corpus verbatim. It scrubs only the PERSISTED copy: the
live in-memory context the agent works from this turn is untouched (a returned COPY is scrubbed, the original
is not), so the agent still functions on the real data while the flywheel never ingests it.

Two tiers: high-confidence SECRETS (private-key blocks, JWTs, provider API keys, bearer / CSRF / session
tokens, emails) and FINANCIAL PII (currency-formatted dollar amounts, user/account IDs, card / SSN numbers).
Dependency-free (stdlib `re` only). OFF -> scrub_record is never called and the trajectory is byte-identical.
"""
import re

_M = "[redacted:{}]"   # visible ASCII marker — shows WHAT was scrubbed, never the value; JSON-safe

# ---- high-confidence SECRETS (order matters: multiline key block first) ----
_SECRET = [
    (re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----", re.S),
     _M.format("private-key")),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{6,}\b"), _M.format("jwt")),
    (re.compile(r"\b(?:tgp_v1_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9]{20,}|ABSK[A-Za-z0-9+/=]{16,}|AKIA[A-Z0-9]{16}|"
                r"tvly-[A-Za-z0-9]{16,}|ghp_[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9-]{10,}|"
                r"AIza[A-Za-z0-9_-]{35})\b"), _M.format("token")),
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{16,}"), "Bearer " + _M.format("token")),
    # a token/secret ASSIGNMENT: <name> = "<value>" / "name":"value" — keep the name, redact the value
    (re.compile(r"(?i)([\"']?(?:_?csrf|csrf[-_]?token|xsrf|session[-_]?token|access[-_]?token|"
                r"refresh[-_]?token|api[-_]?key|secret|password|passwd)[\"']?\s*[:=]\s*[\"'])[^\"']{6,}([\"'])"),
     r"\g<1>" + _M.format("token") + r"\g<2>"),
    # specs/0073: the UNQUOTED form — a .env / YAML / shell-export secret (API_KEY=zk9v..., password: s3cr3t...).
    # The value has no surrounding quotes; require >=6 non-space chars so prose ("password: use a strong one")
    # is left alone, and a negative lookahead for a quote so the quoted branch above owns that case.
    (re.compile(r"(?i)((?:_?csrf|csrf[-_]?token|xsrf|session[-_]?token|access[-_]?token|"
                r"refresh[-_]?token|api[-_]?key|secret|password|passwd)\s*[:=]\s*)"
                r"(?![\"'])([^\s\"';,]{6,})"),
     r"\g<1>" + _M.format("token")),
    (re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"), _M.format("email")),
]

# ---- FINANCIAL PII ----
_FINANCIAL = [
    # currency-formatted dollar amounts ($1,234.56 / $0.00) — the cents anchor keeps a bare "$5" or a
    # "version 1.2" from matching; almost always real money. NO trailing \b: a pasted budget runs the amount
    # straight into the next label ("$1,234.56Mortgage"), where digit->letter is not a word boundary.
    (re.compile(r"\$\s?[\d,]+\.\d{2}"), "$" + _M.format("amount")),
    # user / account / customer id assignments
    (re.compile(r"(?i)([\"']?(?:user|account|customer)[-_]?id[\"']?\s*[:=]\s*[\"'])[A-Za-z0-9]{6,}([\"'])"),
     r"\g<1>" + _M.format("id") + r"\g<2>"),
    # specs/0073: require the GROUPED 4-4-4-4 form (a space/dash between each group) so a solid 16-digit epoch
    # timestamp / id and arbitrary space-separated digit runs don't false-match and corrupt tool output.
    (re.compile(r"\b\d{4}[ -]\d{4}[ -]\d{4}[ -]\d{4}\b"), _M.format("card")),   # 16-digit card (grouped)
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), _M.format("ssn")),
]

_ALL = _SECRET + _FINANCIAL


def scrub_text(s):
    """Apply the secret + financial-PII patterns to one string, returning the scrubbed text. Never raises;
    a marker contains no secret/label so re-scrubbing is a no-op (idempotent enough for re-writes)."""
    if not isinstance(s, str) or not s:
        return s
    out = s
    for rx, repl in _ALL:
        try:
            out = rx.sub(repl, out)
        except Exception:  # noqa: BLE001 - a bad pattern must NEVER drop a trajectory record
            continue
    return out


def scrub_record(obj):
    """Return a scrubbed COPY of a trajectory record — recurse dict/list, scrub string VALUES (keys are field
    names, left alone), pass non-string scalars through. The original object is not mutated, so the live
    context the agent uses this turn is unaffected; only the persisted copy is scrubbed."""
    if isinstance(obj, str):
        return scrub_text(obj)
    if isinstance(obj, dict):
        return {k: scrub_record(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [scrub_record(v) for v in obj]
    return obj
