"""
src/tasks.py

Background task runtime for async workflows (Workflows P3 / specs/0040).

A workflow submitted under CODE_WORKFLOWS_ASYNC runs as a SUBPROCESS (python -m src --run-task ...) that
writes its result to a file; the REPL keeps taking input and drains a completion banner at the next prompt.

Everything here is a PURE, harness-drivable seam EXCEPT popen_spawn (the one real launcher): the TaskRegistry
state machine polls through an INJECTED result-reader and a Popen the caller hands it, so scripts/check_async
exercises the whole lifecycle with a FakePopen + an in-memory dict — no real subprocess, thread, or model.
Stdlib + config only; never imports litellm.
"""
import os
import json
import subprocess

from . import config

QUEUED, RUNNING, DONE, ERROR = "queued", "running", "done", "error"
_TERMINAL = (DONE, ERROR)
_GRACE_POLLS = 3   # tolerate N polls of exited-but-no-result-file (OneDrive visibility lag) before -> error


def _next_state(state, event):
    """PURE transition. An event on a TERMINAL state is a no-op (a late poll can't resurrect or
    double-transition a finished task). events: spawned | still_running | exit_ok_result | exit_ok_grace |
    exit_ok_noresult | exit_fail."""
    if state in _TERMINAL:
        return state
    return {
        "spawned": RUNNING,
        "still_running": RUNNING,
        "exit_ok_result": DONE,
        "exit_ok_grace": RUNNING,     # exited but result not visible yet -> stay running through the grace window
        "exit_ok_noresult": ERROR,
        "exit_fail": ERROR,
    }.get(event, state)


class _Task:
    __slots__ = ("id", "label", "spec_path", "popen", "state", "result", "_polls", "_announced")

    def __init__(self, tid, label, spec_path):
        self.id = tid
        self.label = label
        self.spec_path = spec_path
        self.popen = None
        self.state = QUEUED
        self.result = None       # the dict read from the result file
        self._polls = 0          # exited-but-no-result poll count (grace window)
        self._announced = False  # drained-once flag for the completion banner


# -- result / spec files (under trajectories/tasks/, OUT of the flat *.jsonl glob convert.py reads) --------

def tasks_dir():
    d = os.path.join(config.trajectory_dir(), "tasks")
    os.makedirs(d, exist_ok=True)
    return d


def _atomic_write(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)   # atomic -> a reader never sees a half-written file (OneDrive-safe)


def write_spec(task_id, phases, synthesis, request, parent_session_id):
    path = os.path.join(tasks_dir(), f"{task_id}.spec.json")
    _atomic_write(path, json.dumps({"phases": phases, "synthesis": synthesis,
                                    "request": request, "parent_session_id": parent_session_id}))
    return path


def read_spec(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def write_result(task_id, result):
    _atomic_write(os.path.join(tasks_dir(), f"{task_id}.json"), json.dumps(result))


def read_result(task_id):
    try:
        with open(os.path.join(tasks_dir(), f"{task_id}.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def popen_spawn(workspace):
    """The ONE impure piece: a launcher (task_id, spec_path) -> Popen starting a background --run-task worker.
    A fresh `python -m src --run-task <id> <spec> -C <workspace> --mode plan` subprocess — its own
    Permissions/MCP/litellm globals, READ-ONLY (plan mode), _OAC_BG_WORKER=1 so it can't re-enter the async
    branch. The harness injects a fake spawn instead of calling this."""
    import sys

    def _spawn(task_id, spec_path):
        env = dict(os.environ, _OAC_BG_WORKER="1")
        argv = [sys.executable, "-m", "src", "--run-task", task_id, spec_path, "-C", workspace, "--mode", "plan"]
        return subprocess.Popen(argv, env=env)
    return _spawn


# -- pure formatters (harness-tested) ---------------------------------------------------------------------

def banner_line(task):
    return (f"\n[background] task {task.id} {task.state}: {task.label}"
            + (f"  (/result {task.id} to fold it in)" if task.state == DONE else ""))


def render_tasks(tasks):
    if not tasks:
        return "  (no background tasks)"
    return "\n".join(f"  {t.id}  [{t.state}]  {t.label}" for t in tasks)


def render_result(pulled):
    tid, digest = pulled
    return f"  === background task {tid} ===\n{digest}"


def fold_result(pending, user_text):
    """Fold pulled background digests into the NEXT user task as ONE role:user string (specs/0040): a
    'CONTEXT from ...' preamble + the user's request. It is the SINGLE user message agent.run adds, so it can
    never create a consecutive-user 400, the 0035-fix-C bleed, or a mid-array system turn."""
    if not pending:
        return user_text
    blocks = [f"CONTEXT from completed background task {tid}:\n{digest}" for tid, digest in pending]
    return "\n\n".join(blocks) + "\n\nMy request:\n" + user_text


# -- the registry (state machine over injected poll/read) -------------------------------------------------

class TaskRegistry:
    def __init__(self, read_result=read_result, cap=None):
        self._tasks = {}                 # id -> _Task, insertion-ordered
        self._read_result = read_result  # injected so the harness swaps in an in-memory dict
        self._cap = cap

    def _capacity(self):
        return self._cap if self._cap is not None else config.MAX_BACKGROUND_TASKS

    def non_terminal(self):
        return [t for t in self._tasks.values() if t.state not in _TERMINAL]

    def all_tasks(self):
        return list(self._tasks.values())

    def submit(self, task_id, label, spec_path, spawn):
        """Register + launch a task under a caller-generated id (the spec/result files are keyed by it, so the
        caller writes the spec first). Returns (task_id, None) or (None, error) when the cap is hit or launch
        fails. `spawn(task_id, spec_path) -> popen` is injected (the real popen_spawn, or a fake), so submit
        itself does no file I/O and the harness drives it with a FakePopen."""
        if len(self.non_terminal()) >= self._capacity():
            return None, f"background task cap reached ({self._capacity()}); wait for one to finish (/tasks)."
        t = _Task(task_id, label, spec_path)
        try:
            t.popen = spawn(task_id, spec_path)
        except Exception as e:  # noqa: BLE001 - a launch failure must not crash the turn
            return None, f"could not launch background task: {type(e).__name__}: {e}"
        t.state = _next_state(t.state, "spawned")
        self._tasks[task_id] = t
        return task_id, None

    def refresh(self):
        """Poll every non-terminal task and transition it. Idempotent; safe to call each loop-top."""
        for t in self.non_terminal():
            rc = t.popen.poll() if t.popen is not None else 0
            if rc is None:
                t.state = _next_state(t.state, "still_running")
                continue
            res = self._read_result(t.id)
            if res is not None:
                t.result = res
                ok = rc == 0 and res.get("status") != "error"
                t.state = _next_state(t.state, "exit_ok_result" if ok else "exit_fail")
            elif rc != 0:
                t.state = _next_state(t.state, "exit_fail")
            else:
                t._polls += 1
                t.state = _next_state(t.state, "exit_ok_noresult" if t._polls > _GRACE_POLLS else "exit_ok_grace")

    def drain_finished(self):
        """Banner lines for terminal tasks not yet announced (drain-once)."""
        out = []
        for t in self._tasks.values():
            if t.state in _TERMINAL and not t._announced:
                t._announced = True
                out.append(banner_line(t))
        return out

    def pull(self, tid_prefix):
        """(id, digest) for a DONE task whose id starts with tid_prefix, else None."""
        tid_prefix = (tid_prefix or "").strip()
        if not tid_prefix:
            return None
        for t in self._tasks.values():
            if t.state == DONE and t.id.startswith(tid_prefix):
                return (t.id, (t.result or {}).get("digest") or "(no digest)")
        return None

    def render(self):
        return render_tasks(list(self._tasks.values()))

    def cancel(self, task):
        if task.popen is not None:
            try:
                task.popen.terminate()
                task.popen.wait(timeout=5)
            except Exception:  # noqa: BLE001 - best-effort teardown
                pass
        task.state = ERROR
        task._announced = True
