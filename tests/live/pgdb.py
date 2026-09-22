"""Where the live tests' database is, and whether one is compulsory.

Its own module rather than the conftest, so the conftest and the guard that
reads its verdict are certain to be looking at the same object.
"""
import os

HERE = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.abspath(os.path.join(HERE, "..", ".."))   # platform/engine

# The default is a scratch database of this engine's own. Every session
# drops and rebuilds its schema, so it must never be a database that holds
# anything else -- including, in the monorepo, the platform's.
DEFAULT_URL = ("postgresql+psycopg2://dqengine:dqengine@localhost:5432/"
               "dqengine_test")
URL = os.environ.get("DQENGINE_TEST_DATABASE_URL") or DEFAULT_URL

# the same probe the boundary guard uses: the private api next to us
IN_MONOREPO = os.path.isdir(os.path.join(DIST, "..", "api"))
REQUIRED = IN_MONOREPO or os.environ.get("DQENGINE_REQUIRE_PG") == "1"

# set by the session fixture the moment it gives up, so the run can say so
# in its summary instead of reporting a quiet pass
SKIPPED = False
REASON = ""


def gave_up(reason: str) -> None:
    global SKIPPED, REASON
    SKIPPED, REASON = True, reason
