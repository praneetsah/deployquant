"""The bar store the driver's `BarSource` port reads and writes.

Two consumers have to agree about a day: the engine, which is pushed rows
straight out of the database, and a replay, which reads the zip written
from the same rows. Every rule here exists so those two never disagree --
the in-progress minute is cut at the source, today's zip is rewritten every
call, and a completed day is rewritten whenever its stored rows changed.
"""
import os
import zipfile
from datetime import date, datetime
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from dqengine.live import bar_source, persistence
from dqengine.runtime.core.data import DataStore


class _Rows:
    """A session factory standing in for one bar_days query."""

    def __init__(self, day, rows, symbol=None):
        self._day, self._rows, self._symbol = day, rows, symbol

    def __call__(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def query(self, *a, **k):
        return self

    def filter(self, *a, **k):
        return self

    def all(self):
        rec = type("R", (), {})()
        rec.day = self._day
        rec.rows = self._rows() if callable(self._rows) else self._rows
        if self._symbol is not None:
            rec.symbol = self._symbol
        return [rec]


def test_export_live_bars_rewrites_today_rather_than_caching_it(tmp_path,
                                                                monkeypatch):
    """Today's bars accrue through the session, so a cached file for today
    is stale by construction -- which is the whole reason this runs per
    tick."""
    monkeypatch.setattr(bar_source, "pydata_root", lambda: str(tmp_path))
    monkeypatch.setattr(bar_source, "data_root", lambda: str(tmp_path / "src"))
    bar_source._EXPORTED.clear()
    day = datetime.now(ZoneInfo("America/New_York")).date()   # TODAY, live
    rows = [[34200000, 10.0, 11.0, 9.0, 10.5, 100],
            [34260000, 10.5, 11.5, 10.0, 11.0, 120]]
    written = []
    monkeypatch.setattr(bar_source, "_write_zip_from_rows",
                        lambda p, d, r: written.append((d, len(r))))
    # the in-progress-minute cut reads the wall clock: before 09:32 ET (and
    # all weekend) it drops both 09:30/09:31 rows and nothing is written.
    # This test is about rewrite-vs-cache, so the cut is held still.
    monkeypatch.setattr(bar_source, "_cut_in_progress_minute",
                        lambda rows, day: rows)
    monkeypatch.setattr(bar_source, "SessionLocal", _Rows(day, rows))
    bar_source.export_live_bars(["TQQQ"], day, day)
    bar_source.export_live_bars(["TQQQ"], day, day)
    assert written == [(day, 2), (day, 2)], "today was cached, not rewritten"


def test_export_live_bars_refuses_to_clobber_the_curated_tree(monkeypatch):
    """PYDATA_ROOT unset means the export root IS the curated data root;
    writing raw live rows over parity-proven zips corrupts every backtest
    after."""
    monkeypatch.setattr(bar_source, "pydata_root", lambda: "/tmp/same")
    monkeypatch.setattr(bar_source, "data_root", lambda: "/tmp/same")
    assert bar_source.export_live_bars(["TQQQ"], date(2026, 9, 1),
                                       date(2026, 9, 5)) == 0


def test_live_day_rows_are_the_zip_rows_cut_at_the_in_progress_minute(
        monkeypatch):
    """The engine is pushed live_day_rows; the replay reads the zip. Same
    encoding, same cut, or the two disagree about today."""
    day = date(2026, 9, 4)                                   # not today: no cut
    rows = [[34200000, 10.0, 11.0, 9.0, 10.5, 100],
            [34260000, 10.5, 11.5, 10.0, 11.0, 12345678]]
    monkeypatch.setattr(bar_source, "SessionLocal", _Rows(day, rows, "tqqq"))
    got = bar_source.live_day_rows(["TQQQ"], day)
    assert got == {"TQQQ": bar_source.scaled_rows(rows)}
    assert got["TQQQ"][1][5] == float(f"{12345678:g}"), \
        "volume through %g like the zip"
    # today: the minute that has not ended is dropped, the first minute kept
    now = datetime.now(ZoneInfo("America/New_York"))
    this_min = (now.hour * 3600 + now.minute * 60) * 1000
    cut = bar_source._cut_in_progress_minute(
        [[0, 1, 1, 1, 1, 1], [this_min, 1, 1, 1, 1, 1]], now.date())
    assert [r[0] for r in cut] == [0]
    assert bar_source._cut_in_progress_minute(
        [[this_min, 1, 1, 1, 1, 1]], day) != []


def test_a_completed_day_that_grew_a_late_bar_is_re_exported(tmp_path,
                                                             monkeypatch):
    """A thin symbol's last candle can land after the day's zip was first
    written. "Older days are written once" never looks again, so the two
    readers of that day then differ by a bar -- 39 rows in the table, 38 in
    the zip -- and two engines size a share apart on it. A zip must track
    its rows, not its existence."""
    monkeypatch.setattr(bar_source, "pydata_root", lambda: str(tmp_path))
    monkeypatch.setattr(bar_source, "data_root", lambda: str(tmp_path / "src"))
    bar_source._EXPORTED.clear()
    day = date(2026, 8, 31)                       # a completed day, not today
    rows = [[34200000, 10.0, 11.0, 9.0, 10.5, 100],
            [34260000, 10.5, 11.5, 10.0, 11.0, 120]]
    monkeypatch.setattr(bar_source, "SessionLocal",
                        _Rows(day, lambda: list(rows)))

    def zip_lines():
        p = os.path.join(bar_source._minute_dir(str(tmp_path), "REW"),
                         bar_source._zip_name(day))
        with zipfile.ZipFile(p) as z:
            return z.read(z.namelist()[0]).decode().splitlines()

    assert bar_source.export_live_bars(["REW"], day, day) == 1   # first sight
    assert bar_source.export_live_bars(["REW"], day, day) == 0   # cached
    rows.append([35000000, 11.0, 11.2, 10.9, 11.1, 50])         # the late candle
    assert bar_source.export_live_bars(["REW"], day, day) == 1, \
        "grown day not re-exported"
    assert len(zip_lines()) == 3
    # a process restart forgets the cache: the zip itself is the evidence
    bar_source._EXPORTED.clear()
    assert bar_source.export_live_bars(["REW"], day, day) == 0
    rows.append([35060000, 11.1, 11.3, 11.0, 11.2, 60])
    bar_source._EXPORTED.clear()
    assert bar_source.export_live_bars(["REW"], day, day) == 1
    assert len(zip_lines()) == 4


def test_a_completed_day_whose_bar_was_corrected_in_place_is_re_exported(
        tmp_path, monkeypatch):
    """A feed can resend a corrected candle for a minute it already sent --
    the 09:30 bar once the opening print consolidates, the 15:59 bar with
    the closing prints -- and the writer upserts it by timestamp: same
    count, same minutes, different price. The fingerprint must see the
    content, and survive a restart without re-reading every zip."""
    monkeypatch.setattr(bar_source, "pydata_root", lambda: str(tmp_path))
    monkeypatch.setattr(bar_source, "data_root", lambda: str(tmp_path / "src"))
    bar_source._EXPORTED.clear()
    day = date(2026, 8, 21)
    rows = [[34200000, 10.0, 11.0, 9.0, 10.5, 100],
            [57540000, 10.5, 11.5, 10.0, 11.0, 120]]
    monkeypatch.setattr(bar_source, "SessionLocal",
                        _Rows(day, lambda: [list(x) for x in rows]))

    def zip_close_last():
        p = os.path.join(bar_source._minute_dir(str(tmp_path), "FAS"),
                         bar_source._zip_name(day))
        with zipfile.ZipFile(p) as z:
            return z.read(z.namelist()[0]).decode().splitlines()[-1].split(",")[4]

    assert bar_source.export_live_bars(["FAS"], day, day) == 1
    assert bar_source.export_live_bars(["FAS"], day, day) == 0
    rows[1][4] = 11.29                                # the corrected 15:59 close
    assert bar_source.export_live_bars(["FAS"], day, day) == 1, \
        "in-place edit not re-exported"
    assert zip_close_last() == str(int(round(11.29 * bar_source.SCALE)))
    bar_source._EXPORTED.clear()                      # restart: the sidecar answers
    assert bar_source.export_live_bars(["FAS"], day, day) == 0
    rows[0][1] = 10.02                                # the corrected 09:30 open
    bar_source._EXPORTED.clear()
    assert bar_source.export_live_bars(["FAS"], day, day) == 1


def test_export_bars_roundtrip_from_hist_bars(pg, tmp_path, monkeypatch):
    monkeypatch.setenv("PYDATA_ROOT", str(tmp_path))
    day = date(2026, 6, 1)
    rows = [[34200000 + i * 60000, 10.0 + i, 11.0 + i, 9.0 + i, 10.5 + i, 100.0]
            for i in range(5)]
    with pg() as s:
        s.add(persistence.HistBar(symbol="FAKEPY", day=day, rows=rows))
        s.commit()
    monkeypatch.setattr(bar_source, "SessionLocal", pg)
    n = bar_source.export_bars(["FAKEPY"], day, day)
    assert n == 1
    b = DataStore(str(tmp_path)).load_minute_day("FAKEPY", day)
    assert b is not None and b.n == 5
    assert np.allclose(b.close, [10.5, 11.5, 12.5, 13.5, 14.5])
    assert int(b.start_ms[0]) == 34200000
    # idempotent
    assert bar_source.export_bars(["FAKEPY"], day, day) == 0


def test_export_bars_copies_curated_zips_between_roots(pg, tmp_path,
                                                       monkeypatch):
    src, dst = tmp_path / "src", tmp_path / "dst"
    monkeypatch.setattr(bar_source, "data_root", lambda: str(src))
    monkeypatch.setenv("PYDATA_ROOT", str(dst))
    monkeypatch.setattr(bar_source, "SessionLocal", pg)
    day = date(2026, 6, 1)
    d = src / "equity" / "usa" / "minute" / "curz"
    d.mkdir(parents=True)
    bar_source._write_zip_from_rows(str(d / "20260601_trade.zip"), day,
                                    [[34200000, 1, 2, 0.5, 1.5, 10],
                                     [34260000, 1.5, 2.5, 1.0, 2.0, 10]])
    assert bar_source.export_bars(["CURZ"], day, day) == 1
    assert DataStore(str(dst)).load_minute_day("CURZ", day).n == 2


# ------------------------------------------- the two calls a host supplies

def test_a_bar_source_with_no_history_refuses_rather_than_skipping_it():
    """A host that never wired a history client is a composition error, and
    it has to read as one. Swallowed into `clean=False` it would look like a
    vendor outage and be retried forever; swallowed into `clean=True` every
    warm-up that day would read the hole."""
    from dqengine.live.driver.ports import DriverNotConfigured
    src = bar_source.SqlBarSource()
    with pytest.raises(DriverNotConfigured) as e:
        src.export_history(["TQQQ"], date(2026, 9, 1), date(2026, 9, 4))
    assert "history" in str(e.value)


def test_a_bar_source_with_no_refresh_refuses_rather_than_returning_zero():
    """Zero is a real answer here -- "nothing new to write" -- so an
    uninstalled refresh must not borrow it. The engine would step on
    yesterday's bars and nothing would say so."""
    from dqengine.live.driver.ports import DriverNotConfigured
    src = bar_source.SqlBarSource()
    with pytest.raises(DriverNotConfigured) as e:
        src.refresh("TQQQ")
    assert "refresh" in str(e.value)


def test_the_history_call_is_made_before_the_backtest_export(monkeypatch):
    """The adjusted history is filled first and the zips are written over
    it. The other order splices raw prices under adjusted ones."""
    seen = []
    monkeypatch.setattr(bar_source, "export_bars",
                        lambda *a, **k: seen.append("export"))
    src = bar_source.SqlBarSource(
        history=lambda sym, s0, e0, have: seen.append(("history", sym)))
    assert src.export_history(["TQQQ"], date(2026, 9, 1), date(2026, 9, 4))
    assert seen == [("history", "TQQQ"), "export"]


def test_a_history_client_that_raises_is_loud_and_not_clean(monkeypatch, capsys):
    """Never fatal -- a replay on slightly older bars beats a deployment
    that stops ticking -- but `clean=False` is what makes the driver ask
    again on the next call instead of marking the day done."""
    monkeypatch.setattr(bar_source, "export_bars", lambda *a, **k: 0)

    def boom(sym, s0, e0, have):
        raise RuntimeError("the vendor is down")
    src = bar_source.SqlBarSource(history=boom)
    assert src.export_history(["TQQQ"], date(2026, 9, 1), date(2026, 9, 4)) is False
    assert "history backfill failed" in capsys.readouterr().out


def test_two_writers_of_one_minute_zip_do_not_take_each_others_temp_file(tmp_path):
    """Two deployments sharing a symbol export the same minute zip from two
    workers; with one shared '<zip>.tmp' the second rename found no file
    (seen on the hosted platform, four times a session)."""
    import threading
    from datetime import date
    from dqengine.live import bar_source
    dst = str(tmp_path / "20260924_trade.zip")
    rows = [[34200000, 100.0, 101.0, 99.0, 100.5, 1000]]
    errors, gate = [], threading.Barrier(4)

    def write():
        try:
            gate.wait(timeout=5)
            for _ in range(40):
                bar_source._write_zip_from_rows(dst, date(2026, 9, 24), rows)
                bar_source._write_sidecar(dst, {"n": threading.get_ident()})
        except Exception as e:                              # noqa: BLE001
            errors.append(repr(e))
    ts = [threading.Thread(target=write) for _ in range(4)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert errors == []
    assert not [f for f in tmp_path.iterdir() if f.name.endswith(".tmp")]
