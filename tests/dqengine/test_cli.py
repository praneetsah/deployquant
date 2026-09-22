"""`dqengine` on the command line. The CLI holds no logic of its own: it
parses arguments and calls the same feed, store and backtester the hosted
platform calls, so these tests pin the wiring and the exit codes."""
import json
import os
from datetime import date

import pytest

from dqengine import cli
from dqengine.runtime.core.data import DataStore

ALGO = '''
from AlgorithmImports import *
class A(QCAlgorithm):
    def initialize(self):
        self.set_start_date(2026, 8, 24); self.set_end_date(2026, 8, 25)
        self.set_cash(1000)
        self.s = self.add_equity("AAA", Resolution.MINUTE).symbol
        self.done = False
    def on_data(self, data):
        if not self.done:
            self.done = True
            self.market_order(self.s, 5)
'''


class FakeFeed:
    def __init__(self):
        self.calls = []

    def fetch_days(self, symbol, start, end):
        self.calls.append((symbol, start, end))
        rows = [[34200000 + i * 60000, 100.0, 101.0, 99.0, 100.0 + i, 1000] for i in range(390)]
        return {d: rows for d in (date(2026, 8, 24), date(2026, 8, 25)) if start <= d <= end}


def test_data_fetch_writes_the_store_the_engine_reads(tmp_path, monkeypatch, capsys):
    feed = FakeFeed()
    monkeypatch.setattr(cli, "_alpaca_feed", lambda feed_name: feed)
    rc = cli.main(["data", "fetch", "aaa", "--from", "2026-08-24", "--to", "2026-08-25",
                   "--data", str(tmp_path)])
    assert rc == 0
    assert DataStore(str(tmp_path)).minute_days("AAA") == [date(2026, 8, 24), date(2026, 8, 25)]
    assert "AAA" in capsys.readouterr().out
    # a second run fetches nothing it already has
    feed.calls.clear()
    assert cli.main(["data", "fetch", "AAA", "--from", "2026-08-24", "--to", "2026-08-25",
                     "--data", str(tmp_path)]) == 0
    assert feed.calls == []


def test_data_fetch_without_keys_says_how_to_get_them(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("APCA_API_KEY_ID", raising=False)
    monkeypatch.delenv("APCA_API_SECRET_KEY", raising=False)
    rc = cli.main(["data", "fetch", "AAA", "--from", "2026-08-24", "--data", str(tmp_path)])
    assert rc == 2
    assert "APCA_API_KEY_ID" in capsys.readouterr().err


def test_backtest_prints_a_summary_and_writes_json(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_alpaca_feed", lambda feed_name: FakeFeed())
    cli.main(["data", "fetch", "AAA", "--from", "2026-08-24", "--to", "2026-08-25",
              "--data", str(tmp_path)])
    algo = tmp_path / "algo.py"
    algo.write_text(ALGO)
    out = tmp_path / "result.json"
    rc = cli.main(["backtest", str(algo), "--data", str(tmp_path), "--json", str(out)])
    assert rc == 0
    text = capsys.readouterr().out
    assert "End equity" in text and "Fills" in text
    res = json.loads(out.read_text())
    assert res["stats"]["fills"] == 1 and res["fills"][0]["sym"] == "AAA"


def test_backtest_reports_an_algorithm_error_and_exits_1(tmp_path, capsys):
    algo = tmp_path / "bad.py"
    algo.write_text("def (:")
    assert cli.main(["backtest", str(algo), "--data", str(tmp_path)]) == 1
    assert "SyntaxError" in capsys.readouterr().err


def test_backtest_with_no_data_says_how_to_fetch_it(tmp_path, capsys):
    algo = tmp_path / "algo.py"
    algo.write_text(ALGO)
    assert cli.main(["backtest", str(algo), "--data", str(tmp_path / "empty")]) == 1
    assert "dqengine data fetch AAA" in capsys.readouterr().err


def test_brokers_lists_the_installed_adapters(capsys):
    assert cli.main(["brokers"]) == 0
    out = capsys.readouterr().out
    assert "alpaca-paper" in out and "alpaca" in out


@pytest.fixture()
def engine_mode_env(monkeypatch):
    """`_engine_mode_notice` writes into os.environ on purpose (the runner
    reads the mode at ITS import, which has not happened yet). monkeypatch
    cannot undo a write it did not make, so this puts the two names back --
    left set, they reach every subprocess a later test starts."""
    names = ("PYRUN_ENGINE_MODE", "PYRUN_INPROC_ALLOWED", "PYRUNNER_URL")
    before = {k: os.environ.get(k) for k in names}
    for k in names:
        monkeypatch.delenv(k, raising=False)
    yield
    for k, v in before.items():
        os.environ.pop(k, None)
        if v is not None:
            os.environ[k] = v


def test_live_needs_a_broker(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["live", "algo.py"])
    assert e.value.code == 2
    assert "--broker" in capsys.readouterr().err


def test_live_without_a_bus_says_so_and_changes_no_environment(
        monkeypatch, capsys, engine_mode_env):
    monkeypatch.delenv("REDIS_URL", raising=False)
    assert cli.main(["live", "algo.py", "--broker", "alpaca-paper"]) == 2
    assert "REDIS_URL is not set" in capsys.readouterr().err
    assert "PYRUN_ENGINE_MODE" not in os.environ, \
        "a refusal leaves the process as it found it"


def test_the_engine_mode_defaults_to_in_process_and_says_why(engine_mode_env):
    note = cli._engine_mode_notice()
    assert os.environ["PYRUN_ENGINE_MODE"] == "inproc"
    assert os.environ["PYRUN_INPROC_ALLOWED"] == "1"
    assert "no isolation" in note


def test_a_chosen_engine_mode_is_kept(monkeypatch, engine_mode_env):
    monkeypatch.setenv("PYRUN_ENGINE_MODE", "sandbox")
    assert "sandbox" in cli._engine_mode_notice()
    assert os.environ["PYRUN_ENGINE_MODE"] == "sandbox"


def test_adopt_passes_its_flags_through_and_installs_the_ports(
        monkeypatch, engine_mode_env, capsys):
    """The command holds no logic: the flags reach `adopt.run`, and the
    ports the comparison's replay reads are installed first."""
    import dqengine.live.adopt as adopt_mod
    import dqengine.live.run as run_mod
    seen, installed = {}, []
    monkeypatch.setattr(adopt_mod, "run",
                        lambda *a, **k: seen.update(args=a, kw=k) or 0)
    monkeypatch.setattr(run_mod, "install_ports",
                        lambda **k: installed.append(k))

    rc = cli.main(["adopt", "algo.py", "--broker", "alpaca",
                   "--start", "2026-09-01", "--max-position-usd", "9000",
                   "--no-refresh", "--dry-run"])

    assert rc == 0
    assert seen["args"] == ("algo.py", "alpaca")
    assert seen["kw"]["start"] == date(2026, 9, 1)
    assert seen["kw"]["max_position_usd"] == 9000.0
    assert seen["kw"]["dry_run"] is True
    assert seen["kw"]["refresh_bars"] is False
    assert installed == [{"fallback": "alpaca"}]
    assert "dqengine adopt: algo.py on alpaca" in capsys.readouterr().out


def test_adopt_needs_a_broker(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["adopt", "algo.py"])
    assert e.value.code == 2
    assert "--broker" in capsys.readouterr().err


def test_the_caps_line_says_off_when_no_cap_is_set():
    assert cli._caps_line({}).startswith("off")
    assert "--max-order-usd" in cli._caps_line({"dry_run": True})
    assert "--max-position-usd" in cli._caps_line({})


def test_the_position_cap_flag_is_named_after_what_it_caps():
    """It sets `max_position_notional`, the executor's per-POSITION cap.
    The old name said account, which is not a cap the executor has."""
    args = cli.build_parser().parse_args(
        ["live", "a.py", "--broker", "alpaca", "--max-position-usd", "9000"])
    assert args.max_position_usd == 9000.0
    assert not hasattr(args, "max_account_usd")
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(
            ["live", "a.py", "--broker", "alpaca", "--max-account-usd", "1"])
    from dqengine.live import setup
    assert setup.caps_settings(max_position_usd=9000) == {
        "dry_run": False, "max_position_notional": 9000.0}


def test_the_caps_line_prints_both_caps():
    line = cli._caps_line({"max_order_notional": 2500.0,
                           "max_position_notional": 9000.0})
    assert line == "on: $2,500 per order, $9,000 per position"


def test_version(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["--version"])
    assert e.value.code == 0
    import dqengine
    assert dqengine.__version__ in capsys.readouterr().out


def test_data_fetch_never_defaults_into_the_curated_checkout_tree(tmp_path, monkeypatch, capsys):
    feed = FakeFeed()
    monkeypatch.setattr(cli, "_alpaca_feed", lambda feed_name: feed)
    monkeypatch.setattr(cli, "data_root", lambda: str(tmp_path))
    monkeypatch.setattr(cli, "_checkout_data_root", lambda: str(tmp_path))
    assert cli.main(["data", "fetch", "AAA", "--from", "2026-08-24", "--to", "2026-08-25"]) == 2
    assert "curated" in capsys.readouterr().err and feed.calls == []
    # naming it explicitly is allowed
    assert cli.main(["data", "fetch", "AAA", "--from", "2026-08-24", "--to", "2026-08-25",
                     "--data", str(tmp_path)]) == 0


def test_large_volumes_are_written_as_integers_lean_can_parse(tmp_path):
    import zipfile
    from dqengine.store import write_minute_day
    path = write_minute_day(str(tmp_path), "AAA", date(2026, 8, 24),
                            [[34200000, 1.0, 1.0, 1.0, 1.0, 1270163.0],
                             [34260000, 1.0, 1.0, 1.0, 1.0, 12.5]])
    with zipfile.ZipFile(path) as z:
        rows = z.read(z.namelist()[0]).decode().splitlines()
    assert rows[0].endswith(",1270160") and "e+" not in rows[0]   # 6 significant digits, as ever
    assert rows[1].endswith(",12.5")


def test_example_lists_and_writes_the_shipped_strategies(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["example"]) == 0
    listed = capsys.readouterr().out
    assert "tqqq_weekly" in listed and "sma_trend" in listed and "rsi_dip" in listed
    assert "__init__" not in listed

    assert cli.main(["example", "sma_trend"]) == 0
    written = (tmp_path / "sma_trend.py").read_text()
    assert "class SmaTrend(QCAlgorithm)" in written
    assert "sma_trend.py" in capsys.readouterr().out

    # never overwrite a file the user may have edited
    (tmp_path / "sma_trend.py").write_text("# mine")
    assert cli.main(["example", "sma_trend"]) == 2
    assert (tmp_path / "sma_trend.py").read_text() == "# mine"
    assert "already exists" in capsys.readouterr().err

    assert cli.main(["example", "no_such_thing"]) == 2
    assert "tqqq_weekly" in capsys.readouterr().err       # says what exists
