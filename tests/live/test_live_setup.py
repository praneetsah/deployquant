"""Creating the rows one live deployment runs on, and every refusal.

The refusals are the point of this file. Each one stops a deployment that
would otherwise fail silently -- a daily strategy whose at-close order
nobody can confirm went out, a second strategy nothing can route, a second
deployment on an account the engine cannot fold, real money on a connection
whose fills nobody is checking -- and each one has to leave the database as
it found it.
"""
from datetime import date

import pytest

from dqengine.live import persistence, setup, vault
from dqengine.sandbox import pyrunner

ALGO = '''"""A tiny algorithm."""
from AlgorithmImports import *


class Tiny(QCAlgorithm):
    def initialize(self):
        self.set_start_date(2026, 9, 1)
        self.set_cash(1000)
        self.add_equity("SPY", Resolution.{res})

    def on_data(self, data):
        pass
'''

CREDS = {"key_id": "k", "secret_key": "s", "paper": True}

# a strategy that sizes a position: `set_holdings` is a MARKET order, and on
# daily data a market order placed while the session is open becomes
# market-on-close
SIZING = ('from AlgorithmImports import *\n\n\n'
          'class S(QCAlgorithm):\n'
          '    def on_data(self, data):\n'
          "        self.set_holdings('SPY', 1.0)\n")


@pytest.fixture(autouse=True)
def _inproc(monkeypatch):
    """The manifest pass runs in THIS process here: no docker in a test."""
    monkeypatch.setattr(pyrunner, "ENGINE_MODE", "inproc")


def algo_file(tmp_path, res="MINUTE", name="tiny.py"):
    p = tmp_path / name
    p.write_text(ALGO.format(res=res))
    return str(p)


def prepare(path, **kw):
    kw.setdefault("creds", CREDS)
    kw.setdefault("broker", "fake")
    # the `pg` fixture already built the schema on the test database; the
    # real init_db would reach the engine's own module-level bind
    kw.setdefault("init", False)
    return setup.prepare(path, kw.pop("broker"), **kw)


# ------------------------------------------------------------ the two rows

def test_prepare_creates_one_connection_and_one_deployment(pg, tmp_path):
    out = prepare(algo_file(tmp_path))
    assert out["connection_created"] and out["deployment_created"]
    assert out["universe"] == ["SPY"] and out["resolution"] == "minute"
    with pg() as s:
        assert s.query(persistence.BrokerConnection).count() == 1
        dep = s.query(persistence.Deployment).one()
        assert dep.kind == "python" and dep.code.startswith('"""A tiny')
        assert dep.broker_connection_id == out["conn_id"]
        assert dep.status == "running" and dep.mode == "paper"


def test_a_new_paper_connection_starts_in_observe_and_holds_its_creds(pg,
                                                                     tmp_path):
    out = prepare(algo_file(tmp_path))
    assert out["execution_truth"] == "observe" and out["mode"] == "paper"
    with pg() as s:
        conn = s.query(persistence.BrokerConnection).one()
        assert vault.decrypt_creds(conn.creds_encrypted) == CREDS


def test_rerunning_the_same_file_updates_the_same_rows(pg, tmp_path):
    path = algo_file(tmp_path)
    first = prepare(path, cash=5000.0, start=date(2026, 9, 1))
    open(path, "w").write(ALGO.format(res="MINUTE") + "\n# edited\n")
    second = prepare(path)
    assert (second["conn_id"], second["dep_id"]) == (first["conn_id"],
                                                     first["dep_id"])
    assert not second["connection_created"] and not second["deployment_created"]
    with pg() as s:
        dep = s.query(persistence.Deployment).one()
        assert dep.code.endswith("# edited\n"), "the code snapshot is refreshed"
        assert dep.cash_initial == 5000.0, "cash is not moved without a flag"
        assert dep.start_date == date(2026, 9, 1)


def test_a_named_cash_and_start_do_move_on_a_rerun(pg, tmp_path):
    path = algo_file(tmp_path)
    prepare(path, cash=5000.0, start=date(2026, 9, 1))
    prepare(path, cash=250.0, start=date(2026, 9, 8), margin=2.0)
    with pg() as s:
        dep = s.query(persistence.Deployment).one()
        assert (dep.cash_initial, dep.start_date, dep.margin_max) == (
            250.0, date(2026, 9, 8), 2.0)


# ----------------------------------------------------------- the refusals

def test_a_daily_strategy_needs_an_enforcing_connection(pg, tmp_path):
    """Daily live runs, and this is what it needs from the account. The
    at-close order goes out a minute before the close and the model settles
    the ticket a minute after it; in between only the broker's own execution
    rows say whether it went out, and a fresh connection is `observe`."""
    with pytest.raises(setup.DeploymentRefused) as e:
        prepare(algo_file(tmp_path, res="DAILY"), broker="alpaca")
    assert "enforce" in str(e.value)
    with pg() as s:
        assert s.query(persistence.Deployment).count() == 0
        assert s.query(persistence.BrokerConnection).count() == 0


def test_a_daily_strategy_is_created_on_an_enforcing_connection(pg, tmp_path):
    """The rest of the daily path is the platform's: the replay exports the
    daily zips on its own tick, the payload publishes the at-close order a
    minute before the close, and the model settles it when the bar lands."""
    prepare(algo_file(tmp_path, name="m.py"), broker="alpaca")
    with pg() as s:
        c = s.query(persistence.BrokerConnection).one()
        c.execution_truth = "enforce"
        s.commit()
    out = prepare(algo_file(tmp_path, res="DAILY", name="m.py"),
                  broker="alpaca")
    assert out["resolution"] == "daily"
    with pg() as s:
        assert s.query(persistence.Deployment).one().resolution == "daily"


def test_the_venue_must_carry_the_orders_a_daily_strategy_places(pg,
                                                                 tmp_path):
    """A market order a daily strategy places while the session is open
    becomes market-on-close, which every venue can at least emulate. A venue
    that cannot take the plain market order underneath it is refused here,
    rather than at the strategy's first order."""
    from dqengine.adapters.base import Caps
    from dqengine.live import capabilities
    bad = capabilities.unsupported_for_deploy(
        Caps(order_types=frozenset({"limit"})),
        capabilities.python_order_types(SIZING, daily=True),
        kind="python", daily=True)
    assert [r.order_type for r in bad] == ["market"]
    # and the platform's own limit on carrying an at-close order is lifted,
    # because on daily data the payload does carry it
    assert capabilities.unsupported_for_deploy(
        Caps(order_types=frozenset({"market", "limit"})),
        capabilities.python_order_types(SIZING, daily=True),
        kind="python", daily=True) == []


def test_second_resolution_is_refused(pg, tmp_path):
    with pytest.raises(setup.DeploymentRefused) as e:
        prepare(algo_file(tmp_path, res="SECOND"))
    assert "second-resolution live deployments aren't available yet" in str(e.value)
    with pg() as s:
        assert s.query(persistence.Deployment).count() == 0


def test_check_resolution_lets_minute_and_daily_through():
    assert setup.check_resolution("minute") is None
    assert setup.check_resolution("daily") is None


def test_a_second_deployment_on_the_connection_is_refused(pg, tmp_path):
    prepare(algo_file(tmp_path, name="one.py"))
    with pytest.raises(setup.DeploymentRefused) as e:
        prepare(algo_file(tmp_path, name="two.py"))
    msg = str(e.value)
    assert "'one'" in msg and "one deployment per connection" in msg
    with pg() as s:
        assert s.query(persistence.Deployment).count() == 1, "nothing added"


def test_live_is_refused_unless_the_connection_enforces(pg, tmp_path):
    prepare(algo_file(tmp_path))
    with pytest.raises(setup.DeploymentRefused) as e:
        prepare(algo_file(tmp_path), live=True)
    msg = str(e.value)
    assert "`observe`" in msg and "enforce" in msg
    assert "--dry-run" in msg, "names the way to rehearse against the account"
    assert "dqengine adopt" in msg, "names the command that gets there"
    with pg() as s:
        assert s.query(persistence.BrokerConnection).one().mode == "paper"
        assert s.query(persistence.Deployment).one().live_confirmed is False


def test_live_on_an_enforcing_connection_goes_through(pg, tmp_path):
    out = prepare(algo_file(tmp_path))
    with pg() as s:
        s.get(persistence.BrokerConnection,
              out["conn_id"]).execution_truth = "enforce"
        s.commit()
    again = prepare(algo_file(tmp_path), live=True)
    assert again["mode"] == "live"
    with pg() as s:
        assert s.query(persistence.Deployment).one().live_confirmed is True


def test_live_with_no_connection_at_all_names_that(pg, tmp_path):
    with pytest.raises(setup.DeploymentRefused) as e:
        prepare(algo_file(tmp_path), live=True)
    assert "this broker has no connection here" in str(e.value)
    with pg() as s:
        assert s.query(persistence.BrokerConnection).count() == 0


def test_a_nondeterministic_algorithm_is_refused(pg, tmp_path):
    p = tmp_path / "rand.py"
    p.write_text("import random\n" + ALGO.format(res="MINUTE"))
    with pytest.raises(setup.DeploymentRefused) as e:
        prepare(str(p))
    assert "isn't deterministic" in str(e.value)


def test_a_missing_file_is_refused_before_anything_opens(pg, tmp_path):
    with pytest.raises(setup.DeploymentRefused) as e:
        prepare(str(tmp_path / "nope.py"))
    assert "nope.py" in str(e.value)


# ----------------------------------------------------------------- the caps

def test_caps_are_off_by_default():
    assert setup.caps_settings() == {"dry_run": False}


def test_the_two_cap_flags_write_the_keys_the_rails_read(pg, tmp_path):
    out = prepare(algo_file(tmp_path), max_order_usd=2500,
                  max_position_usd=9000, dry_run=True)
    assert out["settings"] == {"dry_run": True,
                               "max_order_notional": 2500.0,
                               "max_position_notional": 9000.0}
    from dqengine.live.executor import rails_from
    rails = rails_from(out["settings"], "paper", True)
    assert rails.max_order_notional == 2500.0
    assert rails.max_position_notional == 9000.0
    assert rails.dry_run is True


def test_a_rerun_without_the_flags_clears_the_caps_and_keeps_the_rest(pg,
                                                                     tmp_path):
    path = algo_file(tmp_path)
    prepare(path, max_order_usd=2500, dry_run=True)
    with pg() as s:
        conn = s.query(persistence.BrokerConnection).one()
        conn.settings = {**conn.settings, "price_band_pct": 12.0}
        s.commit()
    out = prepare(path)
    assert out["settings"] == {"dry_run": False, "price_band_pct": 12.0}


# ------------------------------------------------------------- the vault key

def test_the_secret_is_never_invented_where_rows_already_exist(pg, tmp_path,
                                                              monkeypatch):
    prepare(algo_file(tmp_path))
    monkeypatch.delenv(vault.SECRET_ENV, raising=False)
    monkeypatch.setattr(vault, "SECRET_PATH", str(tmp_path / "key"))
    with pg() as s:
        with pytest.raises(setup.DeploymentRefused) as e:
            setup.ensure_vault_secret(s)
    assert "already holds 1 encrypted broker connection" in str(e.value)
    assert not (tmp_path / "key").exists(), "no key file was written"


def test_a_first_run_may_write_a_key_and_says_where(pg, tmp_path, monkeypatch):
    monkeypatch.delenv(vault.SECRET_ENV, raising=False)
    path = tmp_path / "key"
    monkeypatch.setattr(vault, "SECRET_PATH", str(path))
    with pg() as s:
        assert setup.ensure_vault_secret(s) == str(path)
    assert path.read_text().strip(), "a real secret, not an empty file"
    assert oct(path.stat().st_mode)[-3:] == "600"


def test_an_available_secret_writes_no_file(pg, tmp_path, monkeypatch):
    monkeypatch.setattr(vault, "SECRET_PATH", str(tmp_path / "key"))
    with pg() as s:
        assert setup.ensure_vault_secret(s) is None
    assert not (tmp_path / "key").exists()


# ------------------------------------------------------------- credentials

def test_credentials_come_from_the_json_variable_when_it_is_set():
    got = setup.creds_from_env("fake", {setup.CREDS_ENV: '{"app_key": "a"}'})
    assert got == {"app_key": "a"}


def test_bad_credential_json_names_the_variable():
    with pytest.raises(setup.DeploymentRefused) as e:
        setup.creds_from_env("fake", {setup.CREDS_ENV: "{oops"})
    assert setup.CREDS_ENV in str(e.value)


def test_an_adapter_that_reads_the_environment_is_asked_first():
    got = setup.creds_from_env("alpaca-paper",
                               {"APCA_API_KEY_ID": "k",
                                "APCA_API_SECRET_KEY": "s"})
    assert got == {"key_id": "k", "secret_key": "s", "paper": True}


def test_a_missing_adapter_variable_is_named():
    with pytest.raises(setup.DeploymentRefused) as e:
        setup.creds_from_env("alpaca-paper", {})
    assert "ALPACA_KEY_ID" in str(e.value)


def test_a_broker_with_no_environment_route_says_to_use_the_json():
    with pytest.raises(setup.DeploymentRefused) as e:
        setup.creds_from_env("alpaca", {})
    assert setup.CREDS_ENV in str(e.value)


# ----------------------------------------------------------- the start date

def test_the_default_start_is_today_before_the_open_and_tomorrow_after():
    from datetime import datetime
    assert setup.default_start(datetime(2026, 9, 21, 8, 0, tzinfo=setup.ET)) \
        == date(2026, 9, 21)
    assert setup.default_start(datetime(2026, 9, 21, 10, 0, tzinfo=setup.ET)) \
        == date(2026, 9, 22)
