"""Inject a fault into a running fixture-mode Settle (PRD §32 deliberate failure demo).

    uv run python scripts/inject_failure.py                      # HubSpot 503 x3 after the Stripe write
    uv run python scripts/inject_failure.py --op update_dispute --times 99   # HubSpot stays down -> escalation
    uv run python scripts/inject_failure.py --clear
"""

from __future__ import annotations

import argparse

import httpx

p = argparse.ArgumentParser()
p.add_argument("--api", default="http://localhost:8000")
p.add_argument("--system", default="hubspot", choices=["stripe", "hubspot", "gmail", "slack"])
p.add_argument("--op", default="update_dispute")
p.add_argument("--mode", default="error", choices=["error", "timeout", "timeout_after_commit", "silent_drop"])
p.add_argument("--status", type=int, default=503)
p.add_argument("--times", type=int, default=3)
p.add_argument("--clear", action="store_true")
a = p.parse_args()
if a.clear:
    print(httpx.delete(f"{a.api}/api/demo/faults").json())
else:
    print(httpx.post(f"{a.api}/api/demo/faults", json={"system": a.system, "op": a.op, "mode": a.mode,
                                                        "status": a.status, "times": a.times}).json())
