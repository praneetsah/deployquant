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


def test_live_is_honest_about_not_shipping_yet(capsys):
    assert cli.main(["live", "algo.py"]) == 2
    assert "0.2" in capsys.readouterr().err


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
