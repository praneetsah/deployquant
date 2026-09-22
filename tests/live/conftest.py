"""The live tests need a Postgres. This finds one, or says why it could not.

The URL comes from `DQENGINE_TEST_DATABASE_URL`, and defaults to a local
`dqengine_test` database:

    createdb dqengine_test
    DQENGINE_TEST_DATABASE_URL=postgresql+psycopg2://me@localhost/dqengine_test \\
        python -m pytest tests

It has to be a database of its own. Every test starts by dropping and
rebuilding the schema, so pointing this at a database that holds anything
you want to keep destroys it.

When no Postgres answers within two seconds the live tests skip, and the
run says so at the end rather than reporting a quiet pass. In this
monorepo, where the platform's own suite sits next to this one, a skip is
a failure instead: see test_pg_never_skips_here.py.
"""
import pytest

pytest.importorskip("sqlalchemy")

from sqlalchemy import create_engine, text          # noqa: E402
from sqlalchemy.orm import sessionmaker             # noqa: E402

import pgdb                                         # noqa: E402


@pytest.fixture(scope="session")
def _pg_engine():
    """Engine on the test database, schema rebuilt once per session so model
    changes always apply. Skips the whole live directory when nothing
    answers."""
    from dqengine.live import persistence
    eng = create_engine(pgdb.URL, connect_args={"connect_timeout": 2})
    try:
        with eng.connect() as c:
            c.execute(text("SELECT 1"))
    except Exception as e:
        eng.dispose()
        pgdb.gave_up(f"{pgdb.URL}: {type(e).__name__}")
        pytest.skip("postgres not reachable")
    with eng.begin() as c:      # drop_all cannot order every cycle
        c.execute(text("DROP SCHEMA public CASCADE"))
        c.execute(text("CREATE SCHEMA public"))
    persistence.Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def pg(monkeypatch, _pg_engine):
    """Clean-tabled session factory, patched onto the one module that owns
    the name. `_sweep_lock_bind` takes the advisory lock on
    `SessionLocal.kw["bind"]`, so the factory it finds has to be the test
    one or the lock is taken on the real database."""
    from dqengine.live import persistence
    factory = sessionmaker(bind=_pg_engine, expire_on_commit=False)
    with _pg_engine.begin() as c:                # clean slate per test
        for t in reversed(persistence.Base.metadata.sorted_tables):
            c.execute(text(f'TRUNCATE TABLE "{t.name}" CASCADE'))
    monkeypatch.setattr(persistence, "SessionLocal", factory)
    yield factory


@pytest.fixture()
def owner_id():
    """Who owns the rows a rig seeds. The engine's tables carry `user_id` as
    a plain nullable tag -- it says nothing to the engine, and there is no
    users table here to point it at. The platform's suite has the same
    fixture returning a real user row, because on that side the column is a
    NOT NULL foreign key."""
    return None


@pytest.fixture(autouse=True)
def _a_secret_for_the_vault(monkeypatch):
    """The vault refuses to invent a key, and rightly: a fresh one silently
    fails to decrypt everything already stored. A test process is a host
    like any other, so it supplies one -- a labelled throwaway, never a
    real secret, and never the file under the home directory."""
    from dqengine.live import vault
    monkeypatch.setattr(vault, "_PROVIDER", None)
    monkeypatch.setenv(vault.SECRET_ENV, "test-suite-not-a-real-secret")


@pytest.fixture(autouse=True)
def _sync_gate_off():
    """The per-connection sweep floor/cooldown (executor._SYNC_GATE) would
    silently skip the second sync of any test that runs two sweeps within
    SYNC_FLOOR_S. Tests exercise pacing explicitly by populating the gate
    themselves; everyone else gets a clean, open gate."""
    from dqengine.live import executor
    executor._SYNC_GATE.clear()
    saved = executor.SYNC_FLOOR_S
    executor.SYNC_FLOOR_S = 0
    yield
    executor.SYNC_FLOOR_S = saved
    executor._SYNC_GATE.clear()


@pytest.fixture(autouse=True)
def _frames_off(monkeypatch):
    """The reconcile recorder (frames.py) inserts from its own thread. A
    thread still holding a session when a test ends hangs the next test's
    TRUNCATE, and its factory is patched per test anyway -- so the whole
    suite runs with the recorder off and test_frames.py turns it back on
    for itself."""
    monkeypatch.setenv("DQENGINE_RECORD_RECONCILE", "0")


@pytest.fixture(autouse=True)
def _books_off(monkeypatch):
    """Per-test isolation for the direct-submit book registry, and the fast
    path OFF by default -- these suites exercise the sweep as the
    transmitter (pre-book semantics); direct-submit tests opt in with
    book.FAST_PATH = True explicitly."""
    from dqengine.live import book as book_mod
    from dqengine.live import executor as _bx
    book_mod._BOOKS.clear()
    _bx._GATHER_CACHE.clear()
    monkeypatch.setattr(book_mod, "FAST_PATH", False)
    # never spawn an engine container from a test process: a warm tick that
    # returns None is the documented "serve the replay this tick" signal.
    from dqengine.live.driver import engine as _lp
    monkeypatch.setattr(_lp, "warm_tick_python", lambda *a, **k: None)
    monkeypatch.setenv("SUBMIT_POOL", "1")   # deterministic order in tests
    monkeypatch.setenv("SUBMIT_STAGGER_MS", "0")
    yield
    book_mod._BOOKS.clear()


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    """A skipped live directory looks exactly like a passing one in the last
    line of a pytest run. Say it out loud instead."""
    if not pgdb.SKIPPED:
        return
    terminalreporter.write_sep("=", "LIVE TESTS DID NOT RUN",
                               red=True, bold=True)
    terminalreporter.write_line(f"no Postgres answered at {pgdb.REASON}")
    terminalreporter.write_line(
        "the executor's tests need one: set DQENGINE_TEST_DATABASE_URL, or "
        "create the default database")
    terminalreporter.write_line(f"  default: {pgdb.DEFAULT_URL}")
