"""
scripts/calibrate_grounding.py

ONLINE calibration for the Tier-2 grounding verifier's reasoning effort (CODE_GROUNDING_EFFORT).
Answers "why not 120b but reasoning on low?" with EVIDENCE instead of a guess: it runs the ACTUAL
verifier subagent against a known honest-but-wrong fixture at low|medium|high and reports, per effort,
catch-rate (the WRONG answer flagged UNGROUNDED) vs false-positive (the GOOD answer flagged). Pick the
cheapest effort that still catches the wrong answer and clears the good one.

Unlike the dep-free check_* harnesses this makes REAL Bedrock calls (needs a working CODE_API_BASE /
credentials) — 6 verifier runs (3 efforts x 2 answers). It is a FAITHFUL calibration because the
runtime verifier reads the LIVE fixture; the deterministic offline curator needs no calibration.

Run:  python scripts/calibrate_grounding.py
"""
import os
import sys
import shutil
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import grounding  # noqa: E402
from src.permissions import Permissions  # noqa: E402
from src.subagent import make_context, run_subagent  # noqa: E402

# The init.sql-class trap from the live ride: the compose MOUNTS docker/auth/init.sql, while
# docker/database/init.sql exists but is a DECOY (never wired). A claim that init lives in the decoy is
# honest-but-wrong — real file, wrong facts — exactly what Tier-2 must catch and Tier-1 provably cannot.
FIXTURE = {
    "docker-compose.yml": (
        "services:\n"
        "  postgres:\n"
        "    image: postgres:15-alpine\n"
        "    volumes:\n"
        "      - ./docker/auth/init.sql:/docker-entrypoint-initdb.d/init.sql:ro\n"),
    "docker/auth/init.sql": "CREATE TABLE users (id serial primary key);\n",
    "docker/database/init.sql": "-- decoy: present but NOT mounted by the compose\n",
    "docker/README.md": "# Docker\nSee docker-compose.yml for the wiring.\n",
}
WRONG = "Postgres is initialized by `docker/database/init.sql` per `docker-compose.yml`."
GOOD = "Postgres is initialized by `docker/auth/init.sql`, which `docker-compose.yml` mounts."
EFFORTS = ["low", "medium", "high"]


def _write_fixture(root):
    for rel, content in FIXTURE.items():
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p) or root, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)


def _verify(answer, ctx, effort):
    """Run the real Tier-2 verifier on `answer` at `effort`; return (flagged: bool)."""
    paths = grounding.cited_paths(answer, strict=False)
    out = run_subagent(grounding._verifier_task(answer, paths), ctx, effort=effort)
    return bool(grounding._parse_verdict(out))


def main():
    fixture = tempfile.mkdtemp(prefix="calib_fixture_")
    traj_dir = tempfile.mkdtemp(prefix="calib_traj_")   # keep calibration runs OUT of the corpus
    _write_fixture(fixture)
    ctx = make_context(fixture, Permissions.from_config(mode_override="bypass"),
                       "calibration", depth=0, verbose=False, traj_dir=traj_dir)

    print("Grounding-verifier effort calibration  (init.sql honest-but-wrong fixture)")
    print(f"  fixture: {fixture}")
    print(f"  {'effort':<8} {'WRONG answer':<18} {'GOOD answer':<18} verdict")
    rows = []
    try:
        for e in EFFORTS:
            wrong_flagged = _verify(WRONG, ctx, e)
            good_flagged = _verify(GOOD, ctx, e)
            ok = wrong_flagged and not good_flagged   # caught the wrong one AND cleared the good one
            rows.append((e, ok))
            print(f"  {e:<8} {('caught' if wrong_flagged else 'MISSED'):<18} "
                  f"{('FALSE-FLAG' if good_flagged else 'clean'):<18} {'PASS' if ok else 'FAIL'}")
    finally:
        shutil.rmtree(fixture, ignore_errors=True)
        shutil.rmtree(traj_dir, ignore_errors=True)

    passing = [e for e, ok in rows if ok]
    if passing:
        print(f"\nCheapest effort that catches the wrong answer AND clears the good one: {passing[0]}")
        print(f"  -> set  CODE_GROUNDING_EFFORT={passing[0]}")
    else:
        print("\nNo effort both caught the wrong answer and cleared the good one — inspect the runs; "
              "the semantic tier may need a stronger judge for this class.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
