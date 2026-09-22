"""Here, a skipped live test is a failed one.

Somewhere -- on a contributor's laptop, in a container with no database --
the executor's tests skip, and that is the right answer: a backtest-only
install of this engine has no Postgres and no reason to have one.

In the monorepo it is the wrong answer. The executor trades real money from
this code, the platform's own suite runs against a database two directories
away, and a green run that quietly exercised none of it is worse than a red
one. So when `platform/api` is next to us, or DQENGINE_REQUIRE_PG=1 is set,
this connects for itself and fails.
"""
import pytest

import pgdb

pytestmark = pytest.mark.skipif(
    not pgdb.REQUIRED,
    reason="no database is required here (no platform/api, no "
           "DQENGINE_REQUIRE_PG=1)")


def test_a_database_answers():
    from sqlalchemy import create_engine, text
    eng = create_engine(pgdb.URL, connect_args={"connect_timeout": 2})
    try:
        with eng.connect() as c:
            assert c.execute(text("SELECT 1")).scalar() == 1
    except Exception as e:
        raise AssertionError(
            f"no Postgres answered at {pgdb.URL} ({type(e).__name__}), and "
            f"here the executor's tests are not allowed to skip.\n"
            f"Point DQENGINE_TEST_DATABASE_URL at a scratch database, or "
            f"create the default one ({pgdb.DEFAULT_URL}).\n"
            f"Its schema is dropped and rebuilt every run: never the "
            f"platform's own database.") from e
    finally:
        eng.dispose()


def test_no_live_test_skipped_for_want_of_a_database():
    """The session fixture sets this the moment it gives up, so a skip that
    happened before this file was reached still fails the run."""
    assert not pgdb.SKIPPED, (
        "the live tests skipped for want of a database; see the summary")
