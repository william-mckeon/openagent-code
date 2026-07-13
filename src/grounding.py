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

# Language that ASSERTS a file / directory / body of code is MISSING, EMPTY, or ABSENT - the
# honest-but-wrong "the auth service has no Go source / the directory is empty / it can't be built"
# class. When the answer makes such a claim we spawn the Tier-2 verifier EVEN IF it cited no explicit
# path (an absence claim names its target only in prose, so it would cite nothing and silently skip the
# check - the live miss where "the auth service has no Go source" passed while src/auth held 14 .go
# files). A false trigger just costs one spawn that returns GROUNDED, so the detector errs toward
# catching the claim (over-triggering is cheap; a missed false-absence is the whole failure mode).
_ABSENCE = re.compile(
    r"\b(?:no|not|without|lacks?|lacking|missing|absent|empty|nonexistent|un(?:implemented|written|available))\b"
    r".{0,40}?"   # a bounded, dot-TOLERANT gap (a '.go' filename must not stop the reach the way [^.] did)
    r"(?:\b(?:sources?|code|implementation|implemented|logic|files?|director(?:y|ies)|folder|module|package|"
    r"tests?|endpoints?|built)\b|\.(?:go|py|ts|tsx|js|jsx|rs|java|rb|sql|sh|c|cpp|h)\b)"
    r"|\b(?:sources?|code|implementation|files?|director(?:y|ies)|folder|module|service)\b"
    r".{0,25}?\b(?:is|are|was|were)\s+(?:empty|missing|absent)\b"
    r"|\bcannot\s+be\s+(?:built|run|compiled|found)\b|\bdoes\s+not\s+exist\b"
    r"|\b(?:only|just|solely)\s+(?:docs?|documentation|config)",
    re.I)


def absence_claim(final_text):
    """True if the closing answer asserts something is missing / empty / absent (see _ABSENCE)."""
    return bool(_ABSENCE.search(final_text or ""))


def absence_contradictions(final_text, cwd):
    """DETERMINISTIC, model-free: flag a claim that a cited path is empty / missing / absent when that
    path actually EXISTS on disk (a present file, or a NON-EMPTY directory). os.path is authoritative for
    the LIVE workspace, so this catches a false absence even when the semantic verifier mis-reads a tree
    (the live 'src/auth/cmd is empty / main.go missing' review, where main.go was read the same session).
    Scoped PER SENTENCE so the path must be claimed absent IN CONTEXT, not merely mentioned elsewhere."""
    if not cwd or not absence_claim(final_text):
        return []
    out, seen = [], set()
    for sent in re.split(r"(?<=[.!?])\s+|\n+", final_text or ""):
        if not _ABSENCE.search(sent):
            continue
        for p in cited_paths(sent, strict=False):   # BROAD: a bare directory citation must count too
            full = os.path.join(cwd, p)
            try:
                if os.path.isfile(full):
                    msg = f"'{p}' is described as missing/empty, but the file EXISTS on disk"
                elif os.path.isdir(full) and os.listdir(full):
                    msg = f"'{p}' is described as empty, but the directory CONTAINS files on disk"
                else:
                    continue
            except OSError:
                continue
            if msg not in seen:
                seen.add(msg)
                out.append(msg)
    return out


def _norm(p):
    """Normalize a path token to the workspace-relative, forward-slash form a CITATION and a piece of
    EVIDENCE are BOTH compared in — so `.\\docker\\README.md`, `./docker/README.md`, and
    `docker/README.md` all match. cited_paths and touched_paths MUST use this identically, or a correct
    citation gets wrongly flagged ungrounded (the offline path-normalization-mismatch bug)."""
    p = (p or "").replace("\\", "/").strip()
    p = p[2:] if p.startswith("./") else p
    return p.strip("/")


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
        if raw.replace("\\", "/").strip().startswith("/"):
            continue                       # absolute path - not judgeable against the workspace; skip
        p = _norm(raw)
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


# Tools that ENGAGE a specific file (its path is in the tool ARGS, untruncated) — as opposed to
# tree/glob/grep, which LIST many files into the (capped, unreliable) result content. touched_paths is
# the offline existence oracle: "which files did the agent actually open/modify?" Shared by the
# grounded_claims rubric check (eval) and train/curate.py, so a citation and its evidence can't drift.
_ENGAGED = {"read_file", "write_file", "edit_file", "delete_file"}


def touched_paths(records):
    """OFFLINE existence oracle: the set of workspace-relative paths a trajectory shows the agent
    actually engaged (read/wrote/edited/deleted), normalized to match cited_paths. Reconstructed from
    ok tool_call ARGS ONLY (never the [:4000]-capped result content), so it is precise but strict — the
    uncontrolled curator layers extra conservatism (a listing hit) on top; a controlled eval fixture
    doesn't need to. Works on a full record list or a single turn's segment."""
    out = set()
    for r in records:
        if r.get("type") == "tool_call" and r.get("ok") and r.get("tool") in _ENGAGED:
            p = (r.get("args") or {}).get("path")
            if p:
                out.add(_norm(p))
    return out


def grounded_by(cited, evidence):
    """True if a cited path is backed by the evidence set — an EXACT normalized match, OR the same
    BASENAME (a file engaged at `src/config.py` but cited in prose as just `config.py`). The basename
    leniency keeps the deterministic check CONSERVATIVE — err toward grounded — so a correct citation is
    never wrongly flagged a phantom just because it named a subdirectory file by its bare name."""
    if cited in evidence:
        return True
    base = cited.rsplit("/", 1)[-1]
    return any(e.rsplit("/", 1)[-1] == base for e in evidence)


def semantic_problems(final_text, paths, spawn, effort=None):
    """Tier 2. Spawn ONE captured verifier subagent to check the answer's factual claims against the
    REAL sources; return the claims it flags ([] == all grounded). spawn(task) -> final text is
    ctx.spawn (the run_subagent path), so the verification is itself captured to the corpus. `effort`
    runs the verifier at a specific reasoning effort (CODE_GROUNDING_EFFORT); it is passed to spawn ONLY
    when set, so a plain 1-arg spawn stub (and the inherit-the-global default) keeps working. Fail-OPEN:
    a missing or errored verdict is logged and treated as "no problems", so an infra hiccup never traps
    the agent in a re-prompt loop (the completion gate already guaranteed the real work was done)."""
    if not spawn:
        return []
    task = _verifier_task(final_text, paths)
    try:
        out = spawn(task, effort=effort) if effort else spawn(task)
    except Exception as e:  # noqa: BLE001 - a verifier failure must never crash the parent turn
        log.warning("grounding verifier raised (%s) - skipping, fail-open", e)
        return []
    if not out or out.strip().startswith("(subagent error"):
        log.warning("grounding verifier gave no usable verdict - skipping, fail-open")
        return []
    return _parse_verdict(out)


def _verifier_task(final_text, paths):
    if paths:
        listed = "Files the answer references:\n" + "\n".join(f"  - {p}" for p in sorted(paths))
    else:
        listed = ("The answer cites no explicit file path - work out which file(s) or director(ies) it "
                  "makes claims about from its text and inspect those yourself (especially anything it "
                  "calls missing, empty, or absent).")
    return (
        "You are a GROUNDING VERIFIER, not a coder. Another agent just finished a task and wrote the "
        "ANSWER below. Your ONLY job is to check whether its factual claims are supported by the ACTUAL "
        "files in this workspace. Read the files it references AND any config they depend on (a "
        "docker-compose / Dockerfile / manifest decides the real wiring - a claim about which file does "
        "X is only true if that config says so). Do NOT perform the task and do NOT suggest "
        "improvements. Flag ONLY a claim a file directly CONTRADICTS (states something DIFFERENT). A "
        "reasonable high-level characterization ('X is a Next.js app' when package.json lists it) is "
        "GROUNDED - do NOT flag a fair summary merely because every detail wasn't exhaustively verified.\n"
        "ONE kind of claim you MUST actively check by looking, not trust: an ABSENCE claim - that a file "
        "or directory is MISSING or EMPTY, or that something 'cannot be built/run', 'has no source', or "
        "'is not implemented'. LIST or open that path YOURSELF; if it actually holds the relevant files, "
        "that absence claim is UNGROUNDED (a real directory the answer wrongly called empty is the "
        "honest-but-wrong class this check exists to catch).\n\n"
        f"{listed}\n\n"
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
    """The re-prompt, sent when grounding finds a problem and a retry remains. Deliberately NARROW and
    NON-HIJACKING: a TARGETED re-check of the flagged claim plus a reminder to still answer the CURRENT
    request. Points at 'the request you're answering now' (the pinned current task), NOT 'your original
    request' — in a multi-turn REPL the latter made the model re-answer an EARLIER turn (a favicon task
    got answered with a prior turn's auth question) after compaction blurred which turn was live."""
    return ("One or more claims in your answer aren't backed by the files:\n"
            + "\n".join(f"- {p}" for p in problems)
            + "\nDo a TARGETED read of just what confirms or corrects each flagged claim - do NOT "
              "re-investigate the whole repo. Then answer the request you are CURRENTLY working on (the "
              "pinned 'current request' above) directly with the claim fixed, keeping the rest of your "
              "answer as-is.")


def problems(final_text, ctx):
    """Runtime entry (Feature B): the live-ctx adapter. Grounding checks ONLY the top-level, user-facing
    answer (ctx.depth == 0). A subagent's answer is intermediate and a Tier-2 verifier must never
    grounding-check ITSELF (its job is to quote paths, incl. ones it asserts are ABSENT), so a depth>0
    agent is skipped entirely — which also means the verifier can't trigger a verify-the-verifier cascade.

    PROPORTIONALITY lives in the verifier's LENIENCY + a non-hijacking challenge, NOT in skipping the
    check. The Tier-2 verifier flags ONLY a CONTRADICTED claim (see _verifier_task), so a fair overview
    ("src/homepage is a Next.js app") is CLEARED — not turned into a repo audit — while a read-only
    REVIEW's honest-but-wrong claim is still caught. (An earlier attempt gated Tier 2 on mutations to
    tame a live run that ballooned a one-line question into 58 tool calls; but that lost the
    honest-but-wrong catch on the highest-value read-only deliverable — a review — so the real fix is the
    lenient verifier + a targeted, non-hijacking challenge, not gating on mutations.)

    Only when semantic is OFF (or no spawn) do we fall back to the deterministic cited-path-existence
    check — which flags only a SPECIFIC missing path, never a bare basename it can't cheaply locate."""
    if getattr(ctx, "depth", 0) != 0:
        return []
    # Deterministic absence contradiction (model-free, authoritative for the live workspace) runs FIRST
    # and ALONGSIDE the semantic verifier — the verifier can mis-read a tree and wrongly agree a path is
    # empty, so os.path.exists is the backstop (the src/auth/cmd main.go case).
    det = absence_contradictions(final_text, getattr(ctx, "cwd", "") or "")
    if config.VERIFY_GROUNDING_SEMANTIC and getattr(ctx, "spawn", None) is not None:
        paths = cited_paths(final_text, strict=False)   # BROAD: the verifier judges, so include dirs
        # Spawn the verifier when the answer cites a path OR makes an ABSENCE claim (which typically
        # names its target only in prose, so it cites no path and would otherwise skip the check).
        if paths or absence_claim(final_text):
            return det + semantic_problems(final_text, paths, ctx.spawn, config.GROUNDING_EFFORT)
        return det
    paths = cited_paths(final_text, strict=True)         # NARROW: a hard existence check must not misfire
    if not paths:
        return det
    muts = getattr(ctx, "mutations", None) or {}
    muts_ci = {os.path.normcase(k) for k in muts}   # case-insensitive on Windows, exact on POSIX

    def _exists(p):
        # A bare basename ('config.py') is often a subdir file (src/config.py) we can't cheaply locate,
        # so NEVER hard-flag it — only a SPECIFIC path (with a slash) missing from disk AND the mutation
        # ledger is a clear phantom (mirrors the offline curator's grounded_by basename leniency).
        return "/" not in p or (os.path.normcase(p) in muts_ci) or os.path.exists(os.path.join(ctx.cwd, p))
    return det + deterministic_problems(paths, _exists)
