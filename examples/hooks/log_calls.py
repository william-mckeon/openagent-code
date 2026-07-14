"""
PostToolUse example hook — telemetry. Appends one line per executed tool call to posttool.log (next to
this script). Observe-only: it reads the result but returns nothing, so it can never alter the tool's
outcome. This is the shape a real flywheel-annotation hook would take (tag the trajectory with an
external signal — did CI pass, did our linter flag).
"""
import json
import os
import sys

try:
    p = json.load(sys.stdin)
except Exception:
    sys.exit(0)

here = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(here, "posttool.log"), "a", encoding="utf-8") as f:
    f.write(f"{p.get('tool')}\t{p.get('target')}\tok={p.get('ok')}\n")
