r"""
src/userdirs.py

Trusted user-typed directories (Phase 35 / specs/0035).

Pure, stdlib-only, side-effect-free. Given a user's REPL line, return the absolute directories the user
LITERALLY typed that are safe to grant READ access to. The whole point is to key a grant off the user's
own text, NOT off a path the model re-typed — a live session showed the model corrupting a typed
`...\OpenCode` into `...\OpenCodeEnvironment`, so `request_dir` failed on a path that never existed.

Design bias: FALSE-NEGATIVE over false-positive. Anything unusual is rejected; the user can always widen
the fence explicitly with `/add-dir`. Nothing here imports from `src` (so the acceptance harness and every
caller can import it with no dependency risk), and nothing here mutates state — the CALLERS (cli.py fix A,
tools.py fix B) gate on config.TRUST_USER_DIRS and do the granting.
"""
import os
import re

# An ANCHORED absolute-path token: a drive-absolute path (C:\... or C:/...) or a UNC path (\\server\...).
# Deliberately NOT a greedy scrape — we match a real absolute prefix and take contiguous non-space,
# non-quote path characters. A drive-RELATIVE `C:foo` (no slash after the colon) is intentionally not
# matched. `[^\s"'`<>|]` stops the token at whitespace, quotes, backticks, and the shell redirection glyphs.
_PATH_TOKEN = re.compile(r"(?:[A-Za-z]:[\\/]|\\\\)[^\s\"'`<>|]*")

# A QUOTED absolute path may legitimately contain SPACES the unquoted token would truncate at
# ("C:\\a\\b c") — capture the whole quoted span (specs/0041 F3).
_QUOTED_PATH = re.compile(r"""["'`]((?:[A-Za-z]:[\\/]|\\\\)[^"'`\n]+?)["'`]""")

# Negation words: a path in a clause introduced by one of these ("don't touch C:\\Windows") is vetoed.
_NEG = re.compile(r"\b(?:not|no|dont|don't|never|avoid|except|excluding|ignore|without|skip|leave)\b", re.I)

# Path COMPONENTS that make a directory sensitive no matter where they appear — credential stores and VCS
# internals. Compared case-folded against every component of the resolved path.
_SENSITIVE_PARTS = {".ssh", ".aws", ".gnupg", ".git", ".config", ".kube", ".docker"}


def _norm(path):
    """Case-folded, backslash-normalized form for comparison (Windows paths are case-insensitive)."""
    return os.path.normcase(path.replace("/", "\\"))


def grantable_dir(path):
    """True if an already-RESOLVED absolute path is a real directory that is SAFE to auto-grant READ access
    to. Applied both to a user-typed token (cli.py) and to a model-supplied request_dir path (tools.py fix
    B), so the denylist can never be bypassed from either side. Conservative — rejects anything shallow,
    system, or credential-bearing even when os.path.isdir is true."""
    if not path or not os.path.isdir(path):
        return False
    rn = _norm(path)

    # 1. reject a bare drive root ("C:\" / "C:") — far too broad to hand over.
    if re.fullmatch(r"[a-z]:\\?", rn):
        return False

    parts = [p for p in rn.strip("\\").split("\\") if p]

    # 2. reject a UNC share root (\\server\share needs at least one dir beneath the share).
    if rn.startswith("\\\\") and len(parts) < 3:
        return False

    # 3. reject a drive path with no component beneath the drive (defensive; (1) usually caught it).
    if re.fullmatch(r"[a-z]:", parts[0] or "") and len(parts) < 2:
        return False

    # 4. credential stores / VCS internals anywhere in the path.
    if any(p in _SENSITIVE_PARTS for p in parts):
        return False

    # 5. system roots (exact match or an ancestor of the path).
    denies = {
        _norm(os.environ.get("SystemRoot", r"C:\Windows")),
        r"c:\windows", r"c:\program files", r"c:\program files (x86)", r"c:\programdata",
    }
    for d in denies:
        if d and (rn == d or rn.startswith(d + "\\")):
            return False

    # 6. the user-profile ROOT itself is too broad (a subdir under it is fine).
    profile = _norm(os.environ.get("USERPROFILE", "")) if os.environ.get("USERPROFILE") else ""
    if profile and rn == profile:
        return False

    return True


def user_typed_dirs(text):
    """The absolute directories the USER literally typed in `text` that are safe to grant READ access to.

    Multi-stage, conservative (bias: false-negative). For each anchored absolute-path token: strip
    surrounding quotes/brackets and trailing sentence punctuation; veto a token whose immediate clause is
    negated ("don't touch ..."); resolve with realpath; keep it only if grantable_dir passes. Returns a
    de-duplicated list, order-preserving. Never raises."""
    if not text:
        return []
    out, seen = [], set()

    def _resolve(raw):
        """Strip surrounding quotes/brackets + trailing sentence punctuation, realpath, and return it iff
        grantable_dir accepts (a real, safe directory); else None."""
        raw = raw.strip().strip("\"'`").strip().rstrip(".,;:!?)]}").strip("\"'`").strip()
        if not raw:
            return None
        try:
            real = os.path.realpath(raw)
        except (OSError, ValueError):
            return None
        return real if grantable_dir(real) else None

    def _emit(real, at):
        # negation veto (the CURRENT clause before the path) + order-preserving dedup
        if _NEG.search(re.split(r"[.\n;]", text[:at])[-1]):
            return
        key = _norm(real)
        if key not in seen:
            seen.add(key)
            out.append(real)

    # 1. a QUOTED absolute path may contain spaces the unquoted token truncates at: take the whole span.
    for m in _QUOTED_PATH.finditer(text):
        real = _resolve(m.group(1))
        if real:
            _emit(real, m.start())

    # 2. an UNQUOTED anchored token stops at the first space (F3: "...\resume helper" -> "...\resume").
    # Extend it word-by-word over the following spaces and grant the LONGEST candidate that is a real,
    # grantable directory — so a spaced folder the user typed beats a shorter same-prefix sibling. A no-space
    # path yields a single candidate, byte-identical to the pre-fix behavior.
    for m in _PATH_TOKEN.finditer(text):
        words, cand, best = text[m.start():].split("\n", 1)[0].split(" "), "", None
        for i, w in enumerate(words):
            if i >= 12:   # word cap so a long sentence can't explode the candidate list
                break
            cand = w if i == 0 else cand + " " + w
            real = _resolve(cand)
            if real:
                best = real   # keep extending; the longest grantable dir wins
        if best:
            _emit(best, m.start())
    return out
