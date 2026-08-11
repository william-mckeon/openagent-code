"""
scripts/check_security_0078.py

Acceptance harness for specs/0078 — secret-exfil hardening (env-scrub + output-scrub + deny-read). Dep-free:
no model, no network. Run:

    python scripts/check_security_0078.py
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import config, envscrub, scrub                 # noqa: E402
from src.tools import Context, grep, glob_tool, tree    # noqa: E402
from src.permissions import Permissions                 # noqa: E402

_results = []


def check(label, cond):
    _results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


def main():
    _saved = {k: getattr(config, k) for k in ("ENV_SCRUB", "SCRUB_OUTPUT", "SECRET_DENY_READ", "ENV_PASSLIST")}

    # -- #1 env-scrub: an allowlisted child env drops CODE_* + secret-shaped vars, keeps PATH ---------------
    base = {"PATH": "/usr/bin", "SYSTEMROOT": "C:/Windows", "CODE_API_KEY": "sk-SECRET",
            "CODE_MODEL": "x", "AWS_SECRET_ACCESS_KEY": "AKIA_SECRET", "MY_TOKEN": "t0k",
            "GITHUB_PASSWORD": "pw", "SOME_RANDOM": "keepme?"}
    config.ENV_SCRUB = True
    e = envscrub.child_env(base)
    check("#1 env-scrub drops CODE_* and secret-shaped vars (api_key/secret/token/password)",
          "CODE_API_KEY" not in e and "CODE_MODEL" not in e and "AWS_SECRET_ACCESS_KEY" not in e
          and "MY_TOKEN" not in e and "GITHUB_PASSWORD" not in e)
    check("#1 env-scrub keeps the shell/toolchain allowlist (PATH, SYSTEMROOT); drops non-allowlisted noise",
          e.get("PATH") == "/usr/bin" and e.get("SYSTEMROOT") == "C:/Windows" and "SOME_RANDOM" not in e)
    config.ENV_PASSLIST = "SOME_RANDOM"
    check("#1 env-scrub: CODE_ENV_PASSLIST re-admits a named var (but never CODE_*)",
          envscrub.child_env(base).get("SOME_RANDOM") == "keepme?"
          and "CODE_API_KEY" not in envscrub.child_env(base))
    config.ENV_PASSLIST = ""
    config.ENV_SCRUB = False
    check("#1 env-scrub OFF -> child_env() is None (run_command inherits the env, byte-identical)",
          envscrub.child_env(base) is None)

    # -- is_secret_path -----------------------------------------------------------------------------------
    config.SECRET_DENY_READ = True
    check("is_secret_path: .env / *.pem / id_rsa match; a source file does not",
          config.is_secret_path(".env") and config.is_secret_path("deploy/prod.pem")
          and config.is_secret_path("id_rsa") and not config.is_secret_path("src/app.py"))

    # -- #6 deny-read: read_file / grep / glob / tree can't surface a designated secret file ---------------
    ws = tempfile.mkdtemp(prefix="sec78_")
    open(os.path.join(ws, ".env"), "w").write("CODE_API_KEY=sk-LEAKLEAKLEAK12345\n")
    open(os.path.join(ws, "app.py"), "w").write("x = 1\n")
    perms = Permissions("bypass", {}, [])
    ctx = Context(ws, perms)
    check("#6 deny-read: read_file(.env) is DENIED, read_file(app.py) is allowed",
          not perms.decide("read_file", {"path": ".env"}, ctx).allowed
          and perms.decide("read_file", {"path": "app.py"}, ctx).allowed)
    check("#6 grep over the workspace does NOT return the .env secret content",
          "sk-LEAK" not in grep({"pattern": "sk-LEAK", "path": "."}, ctx).content)
    check("#6 grep of the .env file DIRECTLY returns no content",
          "sk-LEAK" not in grep({"pattern": "CODE_API_KEY", "path": ".env"}, ctx).content)
    check("#6 glob / tree do NOT list the .env filename",
          ".env" not in glob_tool({"pattern": "**/*", "path": "."}, ctx).content
          and ".env" not in tree({"path": "."}, ctx).content)

    # -- flag OFF is byte-identical: .env is readable/listed again --------------------------------------
    config.SECRET_DENY_READ = False
    check("deny-read OFF -> read_file(.env) is allowed again (byte-identical)",
          perms.decide("read_file", {"path": ".env"}, ctx).allowed
          and "sk-LEAK" in grep({"pattern": "sk-LEAK", "path": "."}, ctx).content)

    # -- #8 output-scrub: scrub_text redacts a key (the wiring reuses this on run_command output) ----------
    check("#8 scrub_text (used for CODE_SCRUB_OUTPUT) redacts a provider key in output",
          "[redacted:token]" in scrub.scrub_text("token: sk-abcdefghij1234567890XYZ done"))

    for k, v in _saved.items():
        setattr(config, k, v)

    passed, total = sum(_results), len(_results)
    print(f"\nVERDICT: {passed}/{total} {'[OK]' if passed == total else '[FAIL]'}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
