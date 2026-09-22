"""The daily zips a DAILY-resolution strategy reads as its bars.

A symbol with no curated daily file gets one derived from its exported
minute days (a DAILY algo over a freshly fetched ticker used to die with
'no data available'), a session still in progress is never derived into a
row, and a curated file is carried past its last day without a single
curated line moving.
"""
import os
import zipfile
from datetime import date

from dqengine.live import bar_source
from dqengine.runtime.core.data import DataStore

OPEN_MS = 9 * 3600_000 + 30 * 60_000


def _mk_minute_day(root, sym, day, rows):
    d = os.path.join(root, "equity", "usa", "minute", sym.lower())
    os.makedirs(d, exist_ok=True)
    bar_source._write_zip_from_rows(
        os.path.join(d, f"{day.strftime('%Y%m%d')}_trade.zip"), day, rows)


def _roots(monkeypatch, tmp_path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir(); dst.mkdir()
    monkeypatch.setattr(bar_source, "data_root", lambda: str(src))
    monkeypatch.setenv("PYDATA_ROOT", str(dst))
    return str(src), str(dst)


def test_daily_derives_from_minute_when_no_curated(monkeypatch, tmp_path):
    src, dst = _roots(monkeypatch, tmp_path)
    # two sessions of minute bars in the DESTINATION tree (export_bars
    # has already run by the time export_daily is called)
    _mk_minute_day(dst, "IYK", date(2026, 6, 1),
                   [(OPEN_MS, 10.0, 11.0, 9.5, 10.5, 100),
                    (OPEN_MS + 60_000, 10.5, 12.0, 10.4, 11.8, 50)])
    _mk_minute_day(dst, "IYK", date(2026, 6, 2),
                   [(OPEN_MS, 11.8, 11.9, 11.0, 11.2, 70)])
    assert bar_source.export_daily(["IYK"]) == 1
    daily = DataStore(dst).load_daily("IYK")
    assert set(daily) == {date(2026, 6, 1), date(2026, 6, 2)}
    o, h, l, c, v = daily[date(2026, 6, 1)]
    assert (o, h, l, c, v) == (10.0, 12.0, 9.5, 11.8, 150.0)
    o2, h2, l2, c2, v2 = daily[date(2026, 6, 2)]
    assert (o2, c2, v2) == (11.8, 11.2, 70.0)


def test_curated_daily_wins_over_derivation(monkeypatch, tmp_path):
    src, dst = _roots(monkeypatch, tmp_path)
    # a curated zip in src AND minute data in dst — the curated copy wins
    ddir = os.path.join(src, "equity", "usa", "daily")
    os.makedirs(ddir)
    with zipfile.ZipFile(os.path.join(ddir, "spy.zip"), "w") as z:
        z.writestr("spy.csv", "20260601 00:00,4000000,4100000,3900000,4050000,999")
    _mk_minute_day(dst, "SPY", date(2026, 6, 1),
                   [(OPEN_MS, 1.0, 1.0, 1.0, 1.0, 1)])
    assert bar_source.export_daily(["SPY"]) == 1
    daily = DataStore(dst).load_daily("SPY")
    assert daily[date(2026, 6, 1)][3] == 405.0     # curated close, not 1.0


def test_no_minute_days_no_zip(monkeypatch, tmp_path):
    _roots(monkeypatch, tmp_path)
    assert bar_source.export_daily(["GHOST"]) == 0


def test_derivation_absorbs_new_days_on_rerun(monkeypatch, tmp_path):
    src, dst = _roots(monkeypatch, tmp_path)
    _mk_minute_day(dst, "TILL", date(2026, 6, 1),
                   [(OPEN_MS, 5.0, 5.5, 4.9, 5.2, 10)])
    bar_source.export_daily(["TILL"])
    _mk_minute_day(dst, "TILL", date(2026, 6, 2),
                   [(OPEN_MS, 5.2, 5.3, 5.0, 5.1, 12)])
    bar_source.export_daily(["TILL"])
    assert set(DataStore(dst).load_daily("TILL")) == {date(2026, 6, 1),
                                                     date(2026, 6, 2)}


# ------------------------------------------------- a day still in progress
#
# A daily bar for a session that has not ended is a close the exchange has
# not printed. The derivation skips today until a minute past its real
# close, which is the same moment the worker rolls the session.

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
TODAY = date(2026, 6, 2)                # a Tuesday, a full session
EARLY = date(2026, 11, 27)              # the day after Thanksgiving, 13:00


def _clock(monkeypatch, when):
    monkeypatch.setattr(bar_source, "_now_et", lambda: when)


def test_a_day_still_in_progress_is_not_derived(monkeypatch, tmp_path):
    src, dst = _roots(monkeypatch, tmp_path)
    _mk_minute_day(dst, "PART", date(2026, 6, 1),
                   [(OPEN_MS, 5.0, 5.5, 4.9, 5.2, 10)])
    _mk_minute_day(dst, "PART", TODAY,
                   [(OPEN_MS, 5.2, 5.3, 5.0, 5.1, 12)])
    _clock(monkeypatch, datetime(2026, 6, 2, 14, 30, tzinfo=ET))
    bar_source.export_daily(["PART"])
    assert set(DataStore(dst).load_daily("PART")) == {date(2026, 6, 1)}


def test_the_day_lands_a_minute_after_the_close(monkeypatch, tmp_path):
    src, dst = _roots(monkeypatch, tmp_path)
    _mk_minute_day(dst, "PART", TODAY, [(OPEN_MS, 5.2, 5.3, 5.0, 5.1, 12)])
    _clock(monkeypatch, datetime(2026, 6, 2, 16, 0, 59, tzinfo=ET))
    bar_source.export_daily(["PART"])
    assert set(DataStore(dst).load_daily("PART")) == set()
    _clock(monkeypatch, datetime(2026, 6, 2, 16, 1, 0, tzinfo=ET))
    bar_source.export_daily(["PART"])
    assert set(DataStore(dst).load_daily("PART")) == {TODAY}


def test_an_early_close_moves_the_moment_with_it(monkeypatch, tmp_path):
    """13:00 + 60 s on a half day, not 16:00 + 60 s."""
    src, dst = _roots(monkeypatch, tmp_path)
    _mk_minute_day(dst, "PART", EARLY, [(OPEN_MS, 5.2, 5.3, 5.0, 5.1, 12)])
    _clock(monkeypatch, datetime(2026, 11, 27, 12, 59, tzinfo=ET))
    bar_source.export_daily(["PART"])
    assert set(DataStore(dst).load_daily("PART")) == set()
    _clock(monkeypatch, datetime(2026, 11, 27, 13, 1, tzinfo=ET))
    bar_source.export_daily(["PART"])
    assert set(DataStore(dst).load_daily("PART")) == {EARLY}


def _many_minute_days(dst, sym, n=15, first=date(2026, 5, 4)):
    """`n` weekday sessions, each one bar, close 5.00, 5.01, ..."""
    days, d = [], first
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
            px = 5.0 + len(days) / 100.0
            _mk_minute_day(dst, sym, d, [(OPEN_MS, px, px, px, px, 10)])
        d += timedelta(days=1)
    return days


def _corrupt_one_day(dst, sym, day):
    """Rewrite that day's line in the daily zip with a wrong close, the way
    a row aggregated from half a session would look."""
    path = os.path.join(dst, "equity", "usa", "daily", f"{sym.lower()}.zip")
    lines, _ = bar_source._read_daily_lines(path)
    stamp = day.strftime("%Y%m%d")
    out = [f"{stamp} 00:00,10000,10000,10000,10000,3" if ln.startswith(stamp)
           else ln for ln in lines]
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(f"{sym.lower()}.csv", "\n".join(out))
    return path


def test_a_settled_day_is_carried_over_and_not_re_read(monkeypatch, tmp_path):
    """Incremental: only the trailing window is re-derived, so a five-year
    symbol costs a directory listing on the tick path rather than 1,250 zip
    reads. Proven by changing a settled row in the file and watching the
    next pass keep it."""
    src, dst = _roots(monkeypatch, tmp_path)
    days = _many_minute_days(dst, "PART")
    bar_source.export_daily(["PART"])
    _corrupt_one_day(dst, "PART", days[0])        # 15 days back, well outside
    bar_source.export_daily(["PART"])
    assert DataStore(dst).load_daily("PART")[days[0]][3] == 1.0


def test_a_partial_row_written_before_is_corrected_once(monkeypatch, tmp_path):
    """The zips already on the volume were written with no completeness
    rule, so some hold a row aggregated from half a session -- and the
    incremental pass above would carry it forever. The version sidecar
    forces one full rebuild, which replaces it."""
    src, dst = _roots(monkeypatch, tmp_path)
    days = _many_minute_days(dst, "PART")
    bar_source.export_daily(["PART"])
    path = _corrupt_one_day(dst, "PART", days[0])
    os.remove(path + ".v")                        # written before the rule
    bar_source.export_daily(["PART"])
    assert DataStore(dst).load_daily("PART")[days[0]][3] == 5.01
    # and the rebuild happens once: the sidecar is back
    assert bar_source._read_daily_version(path) == bar_source.DAILY_DERIVE_VERSION
    _corrupt_one_day(dst, "PART", days[0])
    bar_source.export_daily(["PART"])
    assert DataStore(dst).load_daily("PART")[days[0]][3] == 1.0


def test_a_file_already_correct_is_stamped_without_being_rewritten(
        monkeypatch, tmp_path):
    """The rebuild is forced ONCE. A zip whose rows are already right but
    whose sidecar is missing gets the stamp with no write, so the next pass
    is incremental again instead of re-reading every day forever."""
    src, dst = _roots(monkeypatch, tmp_path)
    _many_minute_days(dst, "PART")
    bar_source.export_daily(["PART"])
    path = os.path.join(dst, "equity", "usa", "daily", "part.zip")
    os.remove(path + ".v")
    before = os.path.getmtime(path)
    assert bar_source.export_daily(["PART"]) == 0
    assert os.path.getmtime(path) == before
    assert bar_source._read_daily_version(path) == bar_source.DAILY_DERIVE_VERSION


def test_a_curated_extension_rebuilds_once_too(monkeypatch, tmp_path):
    src, dst = _roots(monkeypatch, tmp_path)
    _mk_curated(src, text=CURATED.splitlines()[0])       # curated ends 06-01
    days = _many_minute_days(dst, "SPY", n=15, first=date(2026, 6, 2))
    bar_source.export_daily(["SPY"], extend=True)
    path = _corrupt_one_day(dst, "SPY", days[0])
    os.remove(path + ".v")
    bar_source.export_daily(["SPY"], extend=True)
    assert DataStore(dst).load_daily("SPY")[days[0]][3] == 5.01


# ------------------------------------------ extending a curated daily file

CURATED = ("20260601 00:00,4000000,4100000,3900000,4050000,999\n"
           "20260602 00:00,4050000,4200000,4000000,4150000,888")


def _mk_curated(src, sym="SPY", text=CURATED):
    ddir = os.path.join(src, "equity", "usa", "daily")
    os.makedirs(ddir, exist_ok=True)
    with zipfile.ZipFile(os.path.join(ddir, f"{sym.lower()}.zip"), "w") as z:
        z.writestr(f"{sym.lower()}.csv", text)


def _csv(path):
    with zipfile.ZipFile(path) as z:
        return z.read(z.namelist()[0]).decode()


def test_without_extend_a_curated_file_is_copied_and_left_alone(
        monkeypatch, tmp_path):
    """What a minute or second backtest asks for, unchanged."""
    src, dst = _roots(monkeypatch, tmp_path)
    _mk_curated(src)
    _mk_minute_day(dst, "SPY", date(2026, 6, 3),
                   [(OPEN_MS, 1.0, 1.0, 1.0, 1.0, 1)])
    bar_source.export_daily(["SPY"])
    out = os.path.join(dst, "equity", "usa", "daily", "spy.zip")
    assert _csv(out) == CURATED
    assert set(DataStore(dst).load_daily("SPY")) == {date(2026, 6, 1),
                                                    date(2026, 6, 2)}


def test_extend_adds_the_days_after_the_curated_file(monkeypatch, tmp_path):
    src, dst = _roots(monkeypatch, tmp_path)
    _mk_curated(src)
    _mk_minute_day(dst, "SPY", date(2026, 6, 2),          # already curated
                   [(OPEN_MS, 9.0, 9.0, 9.0, 9.0, 1)])
    _mk_minute_day(dst, "SPY", date(2026, 6, 3),
                   [(OPEN_MS, 415.0, 418.0, 414.0, 417.0, 500),
                    (OPEN_MS + 60_000, 417.0, 420.0, 416.5, 419.0, 200)])
    bar_source.export_daily(["SPY"], extend=True)
    daily = DataStore(dst).load_daily("SPY")
    assert set(daily) == {date(2026, 6, 1), date(2026, 6, 2), date(2026, 6, 3)}
    # the curated day keeps the curated numbers, not the minute tree's
    assert daily[date(2026, 6, 2)] == (405.0, 420.0, 400.0, 415.0, 888.0)
    assert daily[date(2026, 6, 3)] == (415.0, 420.0, 414.0, 419.0, 700.0)


def test_the_curated_lines_are_byte_identical_after_an_extension(
        monkeypatch, tmp_path):
    src, dst = _roots(monkeypatch, tmp_path)
    _mk_curated(src)
    _mk_minute_day(dst, "SPY", date(2026, 6, 3),
                   [(OPEN_MS, 415.0, 418.0, 414.0, 417.0, 500)])
    bar_source.export_daily(["SPY"], extend=True)
    out = _csv(os.path.join(dst, "equity", "usa", "daily", "spy.zip"))
    assert out.startswith(CURATED + "\n")
    assert out.splitlines()[:2] == CURATED.splitlines()


def test_extending_twice_changes_nothing(monkeypatch, tmp_path):
    src, dst = _roots(monkeypatch, tmp_path)
    _mk_curated(src)
    _mk_minute_day(dst, "SPY", date(2026, 6, 3),
                   [(OPEN_MS, 415.0, 418.0, 414.0, 417.0, 500)])
    assert bar_source.export_daily(["SPY"], extend=True) == 1
    first = _csv(os.path.join(dst, "equity", "usa", "daily", "spy.zip"))
    assert bar_source.export_daily(["SPY"], extend=True) == 0      # nothing written
    assert _csv(os.path.join(dst, "equity", "usa", "daily", "spy.zip")) == first


def test_an_extension_skips_a_day_still_in_progress(monkeypatch, tmp_path):
    """The live path's whole reason for the rule: at 15:45 a daily strategy
    must not be handed a close the exchange has not printed."""
    src, dst = _roots(monkeypatch, tmp_path)
    _mk_curated(src, text=CURATED.splitlines()[0])       # curated ends 06-01
    _mk_minute_day(dst, "SPY", TODAY, [(OPEN_MS, 415.0, 418.0, 414.0, 417.0, 5)])
    _clock(monkeypatch, datetime(2026, 6, 2, 15, 45, tzinfo=ET))
    bar_source.export_daily(["SPY"], extend=True)
    assert set(DataStore(dst).load_daily("SPY")) == {date(2026, 6, 1)}
    _clock(monkeypatch, datetime(2026, 6, 2, 16, 1, tzinfo=ET))
    bar_source.export_daily(["SPY"], extend=True)
    daily = DataStore(dst).load_daily("SPY")
    assert set(daily) == {date(2026, 6, 1), TODAY}
    assert daily[TODAY] == (415.0, 418.0, 414.0, 417.0, 5.0)


def test_the_extension_refuses_to_rewrite_the_curated_tree(
        monkeypatch, tmp_path, capsys):
    """PYDATA_ROOT unset: the sandbox root IS the curated tree, and the
    extension would rewrite a git-tracked, parity-proven file."""
    one = tmp_path / "one"
    one.mkdir()
    monkeypatch.setattr(bar_source, "data_root", lambda: str(one))
    monkeypatch.setenv("PYDATA_ROOT", str(one))
    _mk_curated(str(one))
    _mk_minute_day(str(one), "SPY", date(2026, 6, 3),
                   [(OPEN_MS, 415.0, 418.0, 414.0, 417.0, 500)])
    bar_source.export_daily(["SPY"], extend=True)
    assert _csv(os.path.join(str(one), "equity", "usa", "daily",
                             "spy.zip")) == CURATED
    assert "extension refused" in capsys.readouterr().out


def test_the_minute_tree_is_untouched_by_a_daily_export(monkeypatch, tmp_path):
    """export_daily reads the minute zips and writes only the daily file."""
    src, dst = _roots(monkeypatch, tmp_path)
    _mk_curated(src)
    _mk_minute_day(dst, "SPY", date(2026, 6, 3),
                   [(OPEN_MS, 415.0, 418.0, 414.0, 417.0, 500)])
    mdir = os.path.join(dst, "equity", "usa", "minute", "spy")
    before = {f: (os.path.getmtime(os.path.join(mdir, f)),
                  open(os.path.join(mdir, f), "rb").read())
              for f in os.listdir(mdir)}
    bar_source.export_daily(["SPY"], extend=True)
    after = {f: (os.path.getmtime(os.path.join(mdir, f)),
                 open(os.path.join(mdir, f), "rb").read())
             for f in os.listdir(mdir)}
    assert after == before


def test_two_writers_of_one_daily_zip_do_not_take_each_others_temp_file(tmp_path):
    """Two daily deployments sharing a symbol export the same zip from two
    workers. With one shared temp name the second rename found no file."""
    import threading
    dst = str(tmp_path / "equity" / "usa" / "daily" / "vxx.zip")
    lines = ["20260105 00:00,500000,510000,490000,505000,1000"]
    errors = []
    gate = threading.Barrier(4)

    def write():
        try:
            gate.wait(timeout=5)
            for _ in range(40):
                bar_source._write_daily_lines(dst, "VXX", lines + [str(threading.get_ident())])
        except Exception as e:                              # noqa: BLE001
            errors.append(repr(e))

    ts = [threading.Thread(target=write) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert errors == []
    assert bar_source._own_tmp(dst) != dst + ".tmp"
    assert not [f for f in (tmp_path / "equity" / "usa" / "daily").iterdir()
                if f.name.endswith(".tmp")]
