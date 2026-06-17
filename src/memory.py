"""
src/memory.py

Cross-session memory (Phase 4 #7) — persistent, per-project understanding.

The agent appends durable notes about a repo with the `remember` tool; those notes
live in a markdown file IN the repo (`<workspace>/.openagent/memory.md` by default),
so memory is per-project automatically. At session start the file is loaded back into
the system prompt, so later runs start with what earlier runs learned instead of cold.

This module is the store only — flat read/append + a load cap. No summarization, no
retrieval, no auto-extraction (see specs/0002-memory.md non-goals). Opt-in via
CODE_MEMORY; OFF for eval so the harness stays isolated.
"""
import os
from datetime import datetime, timezone

from . import config


def path(workspace):
    """Absolute path to this workspace's memory file."""
    return config.memory_file(workspace)


def load(workspace, max_chars=None):
    """Return the project memory text for `workspace`, capped to max_chars (the most
    recent content is kept). Missing file -> "" (never raises)."""
    cap = config.MEMORY_MAX_CHARS if max_chars is None else max_chars
    fp = path(workspace)
    if not os.path.isfile(fp):
        return ""
    try:
        with open(fp, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return ""
    if cap and len(text) > cap:
        # Keep the tail (most recent notes); mark that older memory was elided.
        text = "...(older memory elided)...\n" + text[-cap:]
    return text.strip()


def remember(workspace, note):
    """Append a timestamped note to the project memory file, creating it if needed.
    Returns the file path. The agent's own notebook — not a project edit."""
    note = (note or "").strip()
    if not note:
        return path(workspace)
    fp = path(workspace)
    os.makedirs(os.path.dirname(fp) or ".", exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    header = "" if os.path.isfile(fp) else "# Project memory (openagent-code)\n"
    with open(fp, "a", encoding="utf-8") as f:
        f.write(f"{header}\n- [{stamp}] {note}\n")
    return fp
