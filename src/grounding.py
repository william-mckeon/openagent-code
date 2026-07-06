"""
src/grounding.py

The grounding check (Phase 10 / specs/0010).

Verified completion (specs/0007) proves the agent DID the work — real file changes back the plan.
It cannot prove the work is RIGHT. This adds the next check: are the CLAIMS in the closing answer
grounded in the sources the agent actually cited and touched?

  Tier 1 (deterministic, no model): every file PATH the answer cites must exist — or have been a real
    change target this run. Catches a phantom citation: a path the answer references that isn't real.
  Tier 2 (semantic, harness-driven, opt-in via CODE_VERIFY_GROUNDING_SEMANTIC): spawn ONE CAPTURED
    verifier subagent that re-reads the cited sources and flags factual claims they don't support —
    the honest-but-wrong class (a real path, but the WRONG one per the surrounding files: the
    docker/auth/init.sql-vs-docker/database/init.sql case that slipped past verified completion). The
    verifier is a first-class captured child, so every grounding check also feeds the flywheel.

Change-claims ("I edited X") are DELIBERATELY not re-parsed here: the completion gate already checks
plan steps against the mutation ledger, and specs/0007 anchored on the STRUCTURED plan (not prose) to
avoid brittle NL parsing. This module only checks cited-path EXISTENCE (a safe, literal extraction)
and semantic consistency (delegated to a subagent, never a regex).

Caller-agnostic: pure functions that take evidence + injected callables and return a LIST of problem
strings ([] == grounded == pass). agent.py adapts the live ctx (Feature B); train/curate.py adapts a
trajectory (Feature A, Phase 11). Imports only config + logsetup — no import cycle to break.
"""
import os
import re

from . import config
from .logsetup import get_logger

log = get_logger("grounding")

# A path-like token in the closing answer. cited_paths has TWO strictnesses because its two consumers
# pull in opposite directions: the deterministic tier does a hard existence check with no model, so it
# must be NARROW (a false match wrongly fails a correct answer); the Tier-2 verifier reads the workspace
# and JUDGES, so it must be BROAD (under-inclusion silently skips the honest-but-wrong check).
_QUOTED = re.compile(r"[`'\"]([A-Za-z0-9_.\-/]+)[`'\"]")
_EXT = re.compile(  # known code/doc extensions — the NARROW (deterministic) tier
    r"\.(py|js|ts|tsx|jsx|go|rs|java|rb|c|h|cpp|md|ya?ml|json|toml|sql|sh|txt|env|conf|cfg|ini|lock|xml|html|css)$",
    re.I)
_ANYEXT = re.compile(r"\.[A-Za-z0-9]{1,8}$")                       # any file-ish extension — BROAD tier
_DOMAIN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*\.[A-Za-z]{2,}$")  # github.com, example.io, ...
_DATE = re.compile(r"^\d{1,4}([/-]\d{1,4}){2}$")                   # 2024/01/15


def cited_paths(final_text, strict=False):
    """LOCAL file/dir paths the closing answer references (backtick/quote-wrapped). Two strictnesses:

      strict=False (the Tier-2 verifier — the default caller): BROAD. Any token with a slash (a path or
        a DIRECTORY) or a dot-extension. Over-inclusion (an import slipping in) is harmless — the
        verifier reads the workspace and judges — while UNDER-inclusion would skip the honest-but-wrong
        check for the whole answer (a `docker/auth` directory citation must still spawn the verifier).
      strict=True (the deterministic fallback + Phase-11 offline curation): NARROW. Require a KNOWN file
        extension and drop import-host (`github.com/...`) and date look-alikes, because a hard existence
        check with no model would wrongly fail a correct answer that quotes `lodash/fp` or `2024/01/15`.

    Both exclude URLs, scoped packages, and absolute paths — never workspace-relative files."""
    out = set()
    for m in _QUOTED.finditer(final_text or ""):
        raw = m.group(1)
        if "://" in raw or raw.startswith("@"):
            continue                       # a URL or a scoped package, not a local file
        p = raw.replace("\\", "/").strip()
        if p.startswith("/"):
            continue                       # absolute path - not judgeable against the workspace; skip
        p = p[2:] if p.startswith("./") else p
        p = p.strip("/")
        if not p or p in (".", ".."):
            continue
        if strict:
            if not _EXT.search(p):
                continue                   # known extension only (kills imports/prose in the hard tier)
            if _DATE.match(p) or ("/" in p and _DOMAIN.match(p.split("/", 1)[0])):
                continue                   # a date or an import-host first segment, not a local file
        elif "/" not in p and not _ANYEXT.search(p):
            continue                       # broad tier: a path (slash) or any file-ish extension
        out.add(p)
    return out


def deterministic_problems(paths, exists_fn):
    """Tier 1. Each cited path must be backed by evidence. exists_fn(path) -> bool is injected: the
    runtime checks the live workspace (+ the mutation ledger); the offline curator checks the paths
    the trajectory shows the agent actually touched. Returns human-readable problems ([] == clean)."""
    return [f"'{p}' - cited in the answer but not found in the workspace"
            for p in sorted(paths) if not exists_fn(p)]


def semantic_problems(final_text, paths, spawn):
    """Tier 2. Spawn ONE captured verifier subagent to check the answer's factual claims against the
    REAL sources; return the claims it flags ([] == all grounded). spawn(task) -> final text is
    ctx.spawn (the run_subagent path), so the verification is itself captured to the corpus. Fail-OPEN:
    a missing or errored verdict is logged and treated as "no problems", so an infra hiccup never traps
    the agent in a re-prompt loop (the completion gate already guaranteed the real work was done)."""
    if not spawn or not paths:
        return []
    try:
        out = spawn(_verifier_task(final_text, paths))
    except Exception as e:  # noqa: BLE001 - a verifier failure must never crash the parent turn
        log.warning("grounding verifier raised (%s) - skipping, fail-open", e)
        return []
    if not out or out.strip().startswith("(subagent error"):
        log.warning("grounding verifier gave no usable verdict - skipping, fail-open")
        return []
    return _parse_verdict(out)


def _verifier_task(final_text, paths):
    listed = "\n".join(f"  - {p}" for p in sorted(paths))
    return (
        "You are a GROUNDING VERIFIER, not a coder. Another agent just finished a task and wrote the "
        "ANSWER below. Your ONLY job is to check whether its factual claims are supported by the ACTUAL "
        "files in this workspace. Read the files it references AND any config they depend on (a "
        "docker-compose / Dockerfile / manifest decides the real wiring - a claim about which file does "
        "X is only true if that config says so). Do NOT perform the task and do NOT suggest "
        "improvements. Flag ONLY claims that CONTRADICT or are UNSUPPORTED by the files.\n\n"
        f"Files the answer references:\n{listed}\n\n"
        "=== ANSWER TO VERIFY ===\n" + (final_text or "").strip() + "\n=== END ANSWER ===\n\n"
        "Output one line per problem, exactly:\n"
        "  UNGROUNDED: <the claim, briefly> -> <what the file actually says>\n"
        "If every checkable claim is supported by the files, output exactly one word: GROUNDED")


# Match an UNGROUNDED line-label tolerating the markdown a gpt-oss verifier adds despite "output
# exactly" (leading bullets/quote/heading marks, and **bold**/__italic__ around the label), and CAPTURE
# the claim body verbatim — so decoration inside the claim (`__init__.py`, a `src/**/*.py` glob) is
# preserved for the re-prompt, not mangled.
_UNGROUNDED = re.compile(r"^[\s\-*#>_]*\*{0,2}\s*UNGROUNDED\s*\*{0,2}\s*:\s*(.+)$", re.I)


def _parse_verdict(out):
    problems = []
    for line in out.splitlines():
        m = _UNGROUNDED.match(line.strip())
        if m and m.group(1).strip():
            problems.append(m.group(1).strip())
    return problems


def challenge(problems):
    """The re-prompt (mirror of agent._completion_challenge), sent when grounding finds a problem and
    a retry remains."""
    return ("Do NOT report the task done yet - these claims in your answer aren't grounded in the "
            "actual files:\n" + "\n".join(f"- {p}" for p in problems)
            + "\nRe-check each against the real source (read the file, and any config - compose, "
              "Dockerfile, manifest - that determines the TRUE wiring), then fix your answer or the "
              "code and re-verify. Only report done once every claim matches the files.")


def problems(final_text, ctx):
    """Runtime entry (Feature B): the live-ctx adapter. Grounding checks ONLY the top-level, user-facing
    answer (ctx.depth == 0). A subagent's answer is intermediate — the parent re-checks its own final
    synthesis — and a Tier-2 verifier must never grounding-check ITSELF (its job is to quote paths,
    including ones it asserts are ABSENT, which a path-existence check would wrongly flag). So a depth>0
    agent is skipped entirely; this also means the verifier can't trigger a verify-the-verifier cascade.

    At depth 0: when semantic grounding is on (default) with a spawn available, the verifier subagent is
    the AUTHORITY — it reads the workspace and judges, so it won't false-flag an import/date/prose token
    the way a bare path-existence check would. Only when semantic is OFF (or no spawn) do we fall back to
    the deterministic cited-path-existence check."""
    if getattr(ctx, "depth", 0) != 0:
        return []
    if config.VERIFY_GROUNDING_SEMANTIC and getattr(ctx, "spawn", None) is not None:
        paths = cited_paths(final_text, strict=False)   # BROAD: the verifier judges, so include dirs
        return semantic_problems(final_text, paths, ctx.spawn) if paths else []
    paths = cited_paths(final_text, strict=True)         # NARROW: a hard existence check must not misfire
    if not paths:
        return []
    muts = getattr(ctx, "mutations", None) or {}
    return deterministic_problems(paths, lambda p: (p in muts) or os.path.exists(os.path.join(ctx.cwd, p)))
