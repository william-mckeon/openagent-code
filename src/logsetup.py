"""
src/logsetup.py

The session log — a READABLE, PORTABLE record of a run, built to be handed off for review.

This is distinct from the two recordings that already exist:
  - the TRAJECTORY (src/trajectory.py) — dense machine JSONL with full prompts; the TRAINING data.
  - the REPL prints — live UX, ephemeral, gone when the terminal scrolls.

This is the third thing: one human-readable .log file per run that captures what the agent DID —
every tool call + a result snippet, retries, cold-starts, compaction, errors (with traceback),
and each turn's outcome. The point: run openagent-code on ANY repo, then grab `logs/<run>.log` and
hand it to a reviewer (or paste it to Claude) to debug the run. It NEVER changes agent behavior.

Configured by CODE_LOG_LEVEL / CODE_LOG_DIR (src/config.py). The file handler captures richer
detail (e.g. tool RESULT snippets, latencies) than the terse console UX, on purpose — that detail
is what makes a log reviewable.
"""
import os
import logging

from . import config

_LOG_PATH = None


def configure(run_name):
    """Set up the 'openagent_code' logger to write one readable file per run. Idempotent.
    Returns the absolute log path (or None if logging to a dir is disabled). Never raises."""
    global _LOG_PATH
    root = logging.getLogger("openagent_code")
    root.handlers.clear()
    root.propagate = False
    try:
        level = getattr(logging, (config.LOG_LEVEL or "INFO").upper(), logging.INFO)
    except Exception:
        level = logging.INFO
    root.setLevel(level)

    if not config.LOG_DIR:
        _LOG_PATH = None
        return None
    try:
        os.makedirs(config.LOG_DIR, exist_ok=True)
        path = os.path.abspath(os.path.join(config.LOG_DIR, f"{run_name}.log"))
        fh = logging.FileHandler(path, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
                                          "%H:%M:%S"))
        root.addHandler(fh)
        _LOG_PATH = path
        return path
    except OSError:
        _LOG_PATH = None          # never let logging break a run
        return None


def log_path():
    return _LOG_PATH


def get_logger(component=""):
    """A child logger, e.g. get_logger('model') -> 'openagent_code.model'."""
    return logging.getLogger("openagent_code" + (f".{component}" if component else ""))
