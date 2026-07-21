"""
scripts/check_config_provenance.py

Acceptance harness for specs/0033 - the safety-config fingerprint recorded in each run's session_start, so a
clean guardian-ON run is never indistinguishable from a guardian-OFF one. Dep-free: no model, no network.
Proves the fix and its invariants:

  * config.safety_fingerprint(perms) records the EFFECTIVE permission mode / rule counts / fence width from
    the Permissions object (not the config globals), plus the guard + verify + reach flags.
  * guardian-ON vs guardian-OFF fingerprints DIFFER (the reviewer's gap - the whole point).
  * Trajectory writes session_start.safety when supplied and OMITS it when None (legacy byte-identical).
  * SCHEMA_VERSION is 0.13.0; convert tolerates a legacy (no-safety) AND a fingerprinted session_start.

Run:  python scripts/check_config_provenance.py
"""
import os
import sys
import json
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import config  # noqa: E402
from src.trajectory import Trajectory  # noqa: E402
from src.permissions import Permissions  # noqa: E402
from train import convert  # noqa: E402

_results = []


def check(label, cond):
    _results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


def _mc(content, calls=()):
    tcs = [{"id": str(i), "name": n, "arguments": "{}"} for i, n in enumerate(calls)]
    return {"type": "model_call", "step": 0, "request": {"messages": [], "tools": []},
            "response": {"content": content, "reasoning": None, "tool_calls": tcs}}


def _end(outcome="completed"):
    return {"type": "session_end", "outcome": outcome, "tool_calls": 1}


def main():
    _saved = {"GUARDIAN": config.GUARDIAN}

    # =====================================================================================================
    # 1. the helper reflects the EFFECTIVE perms (mode + fence + rule counts), not the config globals
    # =====================================================================================================
    p = Permissions("acceptEdits",
                    {"deny": ["write_file(secrets/**)", "run_command(rm*)"], "ask": ["edit_file(.env)"], "allow": []},
                    ["/granted/one", "/granted/two"])
    fp = config.safety_fingerprint(p)
    check("fingerprint is a dict recording the guardian flag", isinstance(fp, dict) and "guardian" in fp)
    check("permission_mode comes from perms.mode (acceptEdits), NOT the config global (would be bypass/default)",
          fp["permission_mode"] == "acceptEdits" and config.resolved_permission_mode() != "acceptEdits")
    check("permission_rules records the EFFECTIVE deny/ask/allow counts from perms",
          fp["permission_rules"] == {"deny": 2, "ask": 1, "allow": 0})
    check("extra_roots records the fence width from perms (includes --add-dir grants)", fp["extra_roots"] == 2)
    for k in ("sandbox", "execpolicy", "hooks", "verify_completion", "verify_grounding", "verify_manifest",
              "verify_mutation_claims", "spec_first", "goal_loop", "propose", "enable_web", "mcp_web_active",
              "web_grounding_active", "workdir_prompt"):
        check(f"fingerprint records {k}", k in fp)

    # =====================================================================================================
    # 2. the reviewer's gap: guardian-ON vs guardian-OFF DIFFER
    # =====================================================================================================
    config.GUARDIAN = True
    on = config.safety_fingerprint(p)
    config.GUARDIAN = False
    off = config.safety_fingerprint(p)
    check("a guardian-ON fingerprint DIFFERS from a guardian-OFF one (closes the provenance gap)",
          on != off and on["guardian"] is True and off["guardian"] is False)

    # =====================================================================================================
    # 3. the trajectory records it when supplied, OMITS it when None
    # =====================================================================================================
    ws = os.path.realpath(tempfile.mkdtemp(prefix="prov-"))
    t = Trajectory(ws, "task", "model", ws, safety=config.safety_fingerprint(p))
    t.f.flush()
    ss = json.loads(open(t.path, encoding="utf-8").readline())
    check("session_start records the safety fingerprint when supplied",
          ss.get("safety", {}).get("permission_mode") == "acceptEdits")
    t2 = Trajectory(ws, "task", "model", ws)   # legacy / test construction: no safety
    t2.f.flush()
    ss2 = json.loads(open(t2.path, encoding="utf-8").readline())
    check("session_start OMITS the field when no fingerprint is passed (legacy byte-identical)",
          "safety" not in ss2)

    check("SCHEMA_VERSION bumped to 0.13.0", Trajectory.SCHEMA_VERSION == "0.13.0")

    # =====================================================================================================
    # 4. convert tolerates both a legacy (no safety) and a fingerprinted session_start
    # =====================================================================================================
    legacy = [{"type": "session_start", "schema_version": "0.12.0", "tool_schemas": []},
              _mc("clean", calls=["read_file"]), _end()]
    withfp = [{"type": "session_start", "schema_version": "0.13.0", "tool_schemas": [], "safety": {"guardian": True}},
              _mc("clean", calls=["read_file"]), _end()]
    check("a LEGACY session_start without the safety field still converts (backward-compat)",
          convert.is_trainable(legacy)[0])
    check("a session_start WITH the safety field converts unchanged (tolerated metadata)",
          convert.is_trainable(withfp)[0] and len(convert.to_rows(withfp, "as_sent")) == 1)

    for k, v in _saved.items():
        setattr(config, k, v)

    passed, total = sum(_results), len(_results)
    print(f"\nVERDICT: {passed}/{total} {'[OK]' if passed == total else '[FAIL]'}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
