"""The live-money gate as the executor enforces it.

Deploying to a real-money connection is itself the acknowledgement (the
platform's own half of that is in platform/api/tests/test_live_ack.py).
Auto-acknowledging must not remove the rail: a deployment created outside
the deploy flow -- through the API, or by a migration -- stays blocked until
someone confirms it.
"""
import os

EXEC = os.path.join(os.path.dirname(__file__), "..", "..",
                    "dqengine", "live", "executor.py")


def test_the_rail_still_exists():
    """Auto-acknowledging must not remove the gate itself: a deployment made
    outside the deploy flow has to stay blocked."""
    src = open(EXEC).read()
    assert "live_allowed=(mode != \"live\") or all_live_confirmed" in src
    assert "live-money gate: deployment not confirmed" in src
