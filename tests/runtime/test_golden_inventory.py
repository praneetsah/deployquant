"""The golden fixtures ARE the coverage once the oracle is gone.

Phase 2 proved every generated strategy against the IR engine and froze the
result here; Phase 3 deletes the engine and keeps the freeze. That only
works if the freeze is load-bearing, and it was not: parity._freeze
regenerated a fixture whose .py was missing, so a deleted or renamed golden
disappeared WITHOUT a failure. This file, plus the _freeze change beside it,
is what makes 66 fixtures a gate instead of a cache.

Three directions matter, and each needs its own check:
  * a golden with no NAME  -> dead weight, or a test that was deleted by
    accident and took real coverage with it (test_no_orphan_goldens);
  * a name with no golden  -> the self-heal path, which passes green while
    proving nothing (test_every_pinned_golden_has_a_*);
  * a name and a golden with no CASE -> the hole the first two cannot see.
    Delete a test case, leave its fixture and its name, and everything
    above stays green while nothing replays the strategy. That one is
    test_every_pinned_golden_was_exercised, which reads the set of names
    the harness was ACTUALLY called with during the run.
"""
import os

HERE = os.path.dirname(__file__)
GOLDEN = os.path.join(HERE, "golden")
# Where conftest.pytest_deselected stashes its count. Read off
# `request.config` rather than taken as a fixture: a fixture is only found
# when this directory's conftest is in scope for the item, and collecting out
# of this directory and back into it by explicit file arguments leaves it out
# of scope -- which turned this check into `ERROR ... fixture not found`
# instead of a check. test_the_deselect_counter_is_spelled_the_same_in_both
# pins the spelling.
DESELECTED_ATTR = "_golden_deselected_count"
from dqengine.config import data_root                                  # noqa: E402
DATA = data_root()

# The 48 fixtures frozen by Phase 2 (commits f5c93fe2..7779e616), plus the
# 18 Phase 3 added when the cases that used to be proved by running the IR
# engine alongside became golden replays: four `led_*` (the ledger/adoption
# shapes from test_codegen.py) and fourteen `wt_*` (the weight-tree shapes
# from test_codegen_weight_shapes.py). One name per assert_golden case
# across test_codegen.py, test_codegen_allocation_shapes.py,
# test_codegen_indicators.py, test_codegen_sells.py,
# test_codegen_second_res.py, test_codegen_multi_symbol.py and
# test_codegen_weight_shapes.py.
GOLDEN_NAMES = sorted([
    "alloc_at_open", "alloc_at_time", "alloc_conditional",
    "alloc_guard_position", "alloc_guarded", "alloc_mixed",
    "alloc_partial_tp", "alloc_two_rules", "alloc_weekly",
    "atclose_loc_expires", "atclose_loc_fills", "atclose_moc_all",
    "atclose_trigger", "flat_sell_cancels_target", "ind_atr",
    "ind_cross_symbol", "ind_include_today", "ind_last_of_month",
    "ind_metric_macd", "ind_metric_retargeted", "ind_pct_rank_generic",
    "ind_price_close", "ind_rsi_wilder", "ind_series_sma",
    "liquidate_guarded_exit", "liquidate_releases_guard",
    "multi_cross_symbol", "multi_two_symbol", "rotation_mixed",
    "sec_alloc", "sec_rules", "sell_dollars", "sell_flat_keeps_guard",
    "sell_pct_equity", "sell_pct_position", "sell_ref_prior_close",
    "sell_ref_session_open", "sell_shares",
    "tpl_dca_dip_20240102", "tpl_tqqq_weekly_20240102", "tpl_tqqq_weekly_20240103",
    "tpl_golden_cross_20230103", "tpl_monthly_invest_20240102",
    "tpl_rotation_20240102", "tpl_rotation_20240301",
    "tpl_rotation_20250102", "tpl_rsi_swing_20240102",
    "tpl_sma_trend_20240102",
    # Phase 3, task 5: the adoption/ledger shapes, previously proved by
    # running the IR engine over the same ledger and comparing.
    "led_broker_fills", "led_capped_partials", "led_cash_deposit",
    "led_tagged_row",
    # Phase 3, task 5: the weight-tree shapes, previously proved by running
    # the IR engine over the same tree and comparing.
    "wt_best_cum_return_20240301", "wt_best_cum_return_20250102",
    "wt_equal_20240301", "wt_equal_20250102",
    "wt_if_atr_20240301", "wt_if_atr_20250102",
    "wt_if_rsi_20240301", "wt_if_rsi_20250102",
    "wt_inverse_vol_20240301", "wt_inverse_vol_20250102",
    "wt_nested_best_20240301", "wt_nested_best_20250102",
    "wt_weighted_20240301", "wt_weighted_20250102",
])


# Fixtures in golden/ that are NOT assert_golden cases. acceptance_tqqq_weekly
# is the frozen LEAN-referenced tqqq_weekly result read by
# test_acceptance_tqqq_weekly.py: one .json, no generated .py, because the
# algorithm it pins is a hand-written QC file rather than generated code.
NON_CASE_FIXTURES = {"acceptance_tqqq_weekly"}


def _on_disk(ext):
    return sorted(f[:-len(ext)] for f in os.listdir(GOLDEN)
                  if f.endswith(ext) and f[:-len(ext)] not in NON_CASE_FIXTURES)


def _fixture_files():
    """Every fixture in the directory, counted BY EXTENSION.

    `os.listdir` alone counts whatever else lands in here, and on a Mac a
    single Finder visit drops a `.DS_Store` — which turned the count pin
    below into `assert 98 == 97` on a diff that touched nothing, with
    nothing in the message to say why. The same filter `_on_disk` already
    applies, applied here too."""
    return sorted(f for f in os.listdir(GOLDEN)
                  if f.endswith(".py") or f.endswith(".json"))


def test_every_pinned_golden_has_a_source_fixture():
    missing = sorted(set(GOLDEN_NAMES) - set(_on_disk(".py")))
    assert not missing, (
        f"{len(missing)} golden source fixtures are gone: {missing}. "
        f"Each one is a strategy that was proved against the IR engine and "
        f"is now proved by nothing. Restore them, or delete the case and "
        f"this name together and say why in the commit.")


def test_every_pinned_golden_has_a_digest():
    missing = sorted(set(GOLDEN_NAMES) - set(_on_disk(".json")))
    assert not missing, (
        f"{len(missing)} golden digests are gone: {missing}. The .py proves "
        f"the generator is deterministic; the .json is what proves the code "
        f"still TRADES the same.")


def test_no_orphan_goldens():
    """A fixture nobody asserts against is either dead weight or the
    fingerprint of a test that was deleted by accident."""
    extra = sorted(set(_on_disk(".py")) - set(GOLDEN_NAMES))
    assert not extra, (
        f"golden fixtures with no pinned case: {extra}. Add the name to "
        f"GOLDEN_NAMES when you add a case; delete the file when you delete "
        f"one.")


def test_sources_and_digests_pair_up():
    assert _on_disk(".py") == _on_disk(".json")


def test_the_count_is_pinned():
    """A bare number, so a mass deletion cannot slip through as 'the lists
    still agree with each other'."""
    assert len(GOLDEN_NAMES) == 66
    assert len(_fixture_files()) == 132 + len(NON_CASE_FIXTURES)


def _case_files():
    """The test files that actually drive the golden harness, read off
    disk rather than listed, so adding a file cannot forget to update
    this."""
    me = os.path.basename(__file__)
    out = []
    for f in sorted(os.listdir(HERE)):
        if not (f.startswith("test_") and f.endswith(".py")) or f == me:
            continue
        with open(os.path.join(HERE, f)) as fh:
            if "assert_golden(" in fh.read():
                out.append(f)
    return out


def test_the_deselect_counter_is_spelled_the_same_in_both_places():
    """The count travels from conftest to the check by attribute NAME, so a
    typo on either side is silent: the check would read 0 for ever, the
    deselect guard would stop guarding, and `-k golden` would fail with
    "that strategy is now proved by nothing" about 60-odd strategies that
    were simply not selected. One line of string comparison closes that."""
    with open(os.path.join(HERE, "conftest.py")) as fh:
        conf = fh.read()
    assert f'_COUNT_ATTR = "{DESELECTED_ATTR}"' in conf, (
        f"conftest.py does not stash the deselect count under "
        f"{DESELECTED_ATTR!r}; the orphan check reads that attribute off "
        f"request.config and would silently see 0 deselections.")
    assert "def pytest_deselected(items):" in conf


def test_every_pinned_golden_was_exercised(request):
    """A golden whose TEST CASE was deleted: the hole the two lists above
    cannot see.

    test_no_orphan_goldens catches a fixture with no pinned NAME. Remove
    the CASE instead -- leave the fixture, leave the name -- and every
    other check in this file stays green while nothing replays that
    strategy again. parity.assert_golden records each name it is called
    with; this compares that set to the pin.

    It has to run last, which tests/runtime/conftest.py arranges with a
    hook rather than relying on filenames -- see the reason there.

    THREE guards, because it must never fire on a partial run. A check that
    cries wolf when someone types `-k golden` is a check the next person
    disables, and its message ("that strategy is now proved by nothing") is
    alarming enough to be worth getting right:
      * whole files not collected -- `pytest <one file>`;
      * ANY item deselected -- `-k`, `-m`, `--deselect`. File-level
        collection is not enough on its own: `-k golden` keeps at least one
        item in all seven case files, so the file guard sees a full run
        while hundreds of items never execute. The count comes off
        `request.config`,
        where conftest.pytest_deselected puts it -- NOT off a fixture, which
        this check cannot rely on being in scope (see DESELECTED_ATTR);
      * the bar data the needs_data cases require being absent."""
    import pytest

    import parity

    deselected_count = getattr(request.config, DESELECTED_ATTR, 0)
    ran = {os.path.basename(str(i.path)) for i in request.session.items}
    missing_files = [f for f in _case_files() if f not in ran]
    if missing_files:
        pytest.skip(f"partial run: {len(missing_files)} of "
                    f"{len(_case_files())} golden case files not collected")
    if deselected_count:
        pytest.skip(f"partial run: {deselected_count} items deselected "
                    f"(-k / -m / --deselect), so the set of goldens actually "
                    f"replayed says nothing about the set that exists")
    from conftest_helpers import has_reference_bars
    for sym in ("spy", "qqq", "tqqq"):
        if not has_reference_bars(DATA, sym):
            pytest.skip(f"the reference {sym} bars are not present: the "
                        f"needs_data cases were skipped, so nothing was exercised")

    never_ran = sorted(set(GOLDEN_NAMES) - parity.CALLED)
    assert not never_ran, (
        f"{len(never_ran)} pinned goldens were never replayed: {never_ran}. "
        f"The fixture and the name are both still here, so every other "
        f"check in this file passes -- but no test called assert_golden "
        f"with these names, which means the case was deleted or renamed "
        f"and that strategy is now proved by nothing. Restore the case, or "
        f"delete the case, the name and the fixture together and say why.")
    unpinned = sorted(parity.CALLED - set(GOLDEN_NAMES))
    assert not unpinned, (
        f"assert_golden was called with names that are not in "
        f"GOLDEN_NAMES: {unpinned}. Add them, so the count pin and the "
        f"orphan checks cover them too.")


def test_the_acceptance_reference_is_committed_and_complete():
    """The gate this fixture feeds runs after every task in the one-engine
    cleanup. It used to read a gitignored file that only existed on the
    machine that last ran run_parity.py."""
    import json
    import subprocess
    path = os.path.join(GOLDEN, "acceptance_tqqq_weekly.json")
    ref = json.load(open(path))
    assert len(ref["fills"]) == 520
    assert ref["stats"]["end_equity"] == 13603.38
    assert ref["lean"]["end_equity"] == 13603.38
    in_git = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"],
                            cwd=GOLDEN, capture_output=True).returncode == 0
    if not in_git:           # an sdist or an export snapshot: nothing to ask
        return
    tracked = subprocess.run(["git", "ls-files", "--error-unmatch", path],
                             capture_output=True)
    assert tracked.returncode == 0, f"{path} is not tracked by git"


def test_the_monorepo_always_has_the_reference_bars():
    """The pinned-dollar cases skip when the reference bars are absent, which
    is right for a stranger's clone and WRONG here: in the authors' checkout a
    sentinel that stops matching (a day overwritten by a vendor fetch, a bad
    merge of the data tree) would skip every one of them, this file's
    exercised-goldens check included, and the suite would stay green having
    proved nothing. Where the curated tree exists, it must be the reference."""
    import pytest
    from conftest_helpers import has_reference_bars
    from dqengine.config import _checkout_data_root
    curated = _checkout_data_root()
    if curated is None or os.path.abspath(DATA) != curated:
        pytest.skip("not running against the authors' curated data tree")
    missing = [s for s in ("spy", "qqq", "tqqq") if not has_reference_bars(curated, s)]
    assert not missing, f"curated bars no longer match reference_bars.json: {missing}"

