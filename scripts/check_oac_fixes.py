r"""
scripts/check_oac_fixes.py

Acceptance harness for specs/0042 — two OAC fixes surfaced by the live Centpilot build run:
  Fix A: the --mode LAUNCH FLAG is validated (a `--mode perpose` typo is rejected with a 'propose' hint,
         never silently accepted into a dead permission mode).
  Fix B: grounding skips its PATH checks on a GREENFIELD workspace (an empty project dir), where every
         cited path is a file the answer PROPOSES to create, not a phantom present-state citation.

Dep-free CORE (stdlib + src, NEVER litellm): the greenfield guard is exercised by importing src.grounding
directly, and the --mode contract against config._MODES. The REAL cli.main() rejection needs the model
stack (importing src pulls litellm via runtime->model), so it runs as a SUBPROCESS and is SKIPPED (not
failed) when litellm is absent (system python). Run under .venv for full coverage:

    .venv\Scripts\python.exe scripts/check_oac_fixes.py
"""
import os
import sys
import tempfile
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import config, grounding   # noqa: E402

_results = []


def check(label, cond):
    _results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


def skip(label):
    print(f"  [SKIP] {label}")


class _Ctx:
    """The minimal live-ctx surface grounding.problems() reads via getattr."""
    def __init__(self, cwd):
        self.cwd = cwd
        self.depth = 0
        self.spawn = None
        self.mutations = {}
        self.fetched = {}
        self._verified_ok = False


class _SpyCtx(_Ctx):
    """A ctx whose spawn RECORDS that the Tier-2 verifier was invoked (so we can prove greenfield hands it
    no paths and therefore never spawns for a pure path-citing answer)."""
    def __init__(self, cwd):
        super().__init__(cwd)
        self.spawned = False
        self.spawn = self._spawn

    def _spawn(self, *a, **k):
        self.spawned = True
        return None   # semantic_problems is fail-open on a missing verdict -> []


def _has_litellm():
    try:
        import litellm  # noqa: F401
        return True
    except Exception:
        return False


def main():
    save = (config.GROUND_SKIP_GREENFIELD, config.VERIFY_GROUNDING_SEMANTIC, config.VERIFY_GROUNDING_PATHS)
    config.VERIFY_GROUNDING_PATHS = False   # keep the deterministic extractor in its plain strict form

    with tempfile.TemporaryDirectory() as empty, tempfile.TemporaryDirectory() as populated:
        # empty: only a .git dir + a dotfile -> still greenfield (neither is a reviewable source file)
        os.mkdir(os.path.join(empty, ".git"))
        with open(os.path.join(empty, ".gitignore"), "w") as f:
            f.write("*.pyc\n")
        # populated: one real source file -> NOT greenfield
        with open(os.path.join(populated, "app.py"), "w") as f:
            f.write("x = 1\n")

        # ---- is_greenfield unit ------------------------------------------------------------------------
        check("is_greenfield: empty dir (only .git/.gitignore) -> True", grounding.is_greenfield(empty) is True)
        check("is_greenfield: dir with app.py -> False", grounding.is_greenfield(populated) is False)
        check("is_greenfield: nonexistent path -> False",
              grounding.is_greenfield(os.path.join(empty, "nope")) is False)

        proposal = "I will create `src/budget/allocations.py` and `src/budget/models.py` for the app."

        # ---- deterministic path (semantic OFF): OFF flags proposals, ON suppresses ---------------------
        config.VERIFY_GROUNDING_SEMANTIC = False
        config.GROUND_SKIP_GREENFIELD = False
        off = grounding.problems(proposal, _Ctx(empty))
        check("flag OFF + greenfield: the proposed files ARE flagged (byte-identical to today)",
              any("allocations.py" in p for p in off) and any("models.py" in p for p in off))

        config.GROUND_SKIP_GREENFIELD = True
        on = grounding.problems(proposal, _Ctx(empty))
        check("flag ON + greenfield: the proposed files are NOT flagged", on == [])

        # guard is greenfield-ONLY: a missing cited path in a POPULATED repo is still a phantom
        pop = grounding.problems("See `src/does_not_exist.py` for the config.", _Ctx(populated))
        check("flag ON + populated repo: a missing cited path is STILL flagged",
              any("does_not_exist.py" in p for p in pop))

        # ---- threshold (specs/0047): a small early scaffold counts as greenfield when max_files is raised
        for i in range(4):
            with open(os.path.join(populated, f"stub{i}.js"), "w") as f:
                f.write("// stub\n")
        # populated now holds app.py + 4 stubs = 5 reviewable files
        check("is_greenfield: 5-file scaffold, max_files=0 -> False (empty-only default, byte-identical)",
              grounding.is_greenfield(populated, 0) is False)
        check("is_greenfield: 5-file scaffold, max_files=10 -> True (early-stage counts as greenfield)",
              grounding.is_greenfield(populated, 10) is True)
        check("is_greenfield: 5-file scaffold, max_files=3 -> False (over threshold)",
              grounding.is_greenfield(populated, 3) is False)
        saved_max = config.GROUND_GREENFIELD_MAX
        config.GROUND_SKIP_GREENFIELD = True
        config.GROUND_GREENFIELD_MAX = 10
        prop = grounding.problems("Adding `src/monthly-reset.js` and `src/sinking-funds.js`.", _Ctx(populated))
        check("threshold ON: proposed files in a small scaffold are NOT flagged", prop == [])
        config.GROUND_GREENFIELD_MAX = 0
        still = grounding.problems("See `src/monthly-reset.js`.", _Ctx(populated))
        check("threshold default (0): the same scaffold IS treated as populated (flagged)",
              any("monthly-reset.js" in p for p in still))
        config.GROUND_GREENFIELD_MAX = saved_max

        # ---- semantic branch (verifier ON): greenfield hands it NO paths -> no spawn --------------------
        config.VERIFY_GROUNDING_SEMANTIC = True
        config.GROUND_SKIP_GREENFIELD = True
        spy_green = _SpyCtx(empty)
        grounding.problems(proposal, spy_green)
        check("flag ON + greenfield + semantic: the Tier-2 verifier is NOT spawned for proposed paths",
              spy_green.spawned is False)

        config.GROUND_SKIP_GREENFIELD = False
        spy_pop = _SpyCtx(populated)
        grounding.problems("See `src/does_not_exist.py`.", spy_pop)
        check("flag OFF + populated + semantic: the verifier IS spawned (unchanged behavior)",
              spy_pop.spawned is True)

    config.GROUND_SKIP_GREENFIELD, config.VERIFY_GROUNDING_SEMANTIC, config.VERIFY_GROUNDING_PATHS = save

    # ---- default proven against the fallback, not this repo's live .env --------------------------------
    _env = os.environ.pop("CODE_GROUND_SKIP_GREENFIELD", None)
    default_off = config._as_bool(os.environ.get("CODE_GROUND_SKIP_GREENFIELD", "false")) is False
    if _env is not None:
        os.environ["CODE_GROUND_SKIP_GREENFIELD"] = _env
    check("CODE_GROUND_SKIP_GREENFIELD defaults False when unset (opt-in)", default_off)

    _envm = os.environ.pop("CODE_GROUND_GREENFIELD_MAX", None)
    default_zero = int(os.environ.get("CODE_GROUND_GREENFIELD_MAX", "0")) == 0
    if _envm is not None:
        os.environ["CODE_GROUND_GREENFIELD_MAX"] = _envm
    check("CODE_GROUND_GREENFIELD_MAX defaults 0 when unset (empty-only, byte-identical)", default_zero)

    # ============ Fix A: --mode launch-flag validation ================================================
    check("_MODES is the 5 real modes incl. propose",
          config._MODES == {"default", "acceptEdits", "plan", "bypass", "propose"})
    check("'perpose' is NOT a valid mode (the typo that sailed through before)", "perpose" not in config._MODES)

    if _has_litellm():
        r = subprocess.run([sys.executable, "-m", "src", "--mode", "perpose", "noop-task"],
                           cwd=ROOT, capture_output=True, text=True, timeout=120)
        out = r.stdout + r.stderr
        check("cli --mode perpose -> exit 2 (rejected, not silently accepted)", r.returncode == 2)
        check("cli --mode perpose -> the message suggests 'propose'", "propose" in out)
        check("cli --mode perpose -> lists the valid modes", "valid modes" in out)
    else:
        skip("cli --mode integration (needs litellm; run under .venv for this one)")

    passed, total = sum(_results), len(_results)
    print(f"\nVERDICT: {passed}/{total} {'[OK]' if passed == total else '[FAIL]'}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
