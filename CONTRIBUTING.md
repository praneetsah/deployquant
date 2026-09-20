# Contributing

Bug reports, broker adapters, more indicators, more of the algorithm API and
documentation fixes are all welcome.

If you want a feature that is not supported yet, open an issue. Requests decide
what gets built next.

## Setup

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[sandbox]" -e plugins/dqengine-webull -e plugins/dqengine-schwab pytest httpx
python -m pytest tests -q
```

Most tests use synthetic data and run anywhere. The tests that check exact
dollar results need one specific set of bars, which cannot be redistributed.
They skip if you do not have that set.

## Rules for changes

1. The tests have to pass: `python -m pytest tests -q`.
2. Do not change an expected value in a test to make your change pass. This
   applies to the fill-by-fill comparison with LEAN and to the saved strategy
   results in `tests/runtime/golden/`. If an expected value really has to move,
   explain why in the pull request.
3. Anything the engine does not support has to raise `UnsupportedApiError` with
   the name of the feature. A method must never accept an argument and ignore
   it.
4. `tests/test_boundary.py` checks how the packages depend on each other.
   `dqengine.runtime` may not import the rest of `dqengine`. The engine may not
   import a broker plugin. Importable code may not change `sys.path`.
5. New indicators need reference values recorded from LEAN with
   `tools/lean_reference.py`. Values from a textbook formula are not accepted,
   because the goal is to match LEAN.
6. Changes to order placement, fills, the order journal, reconciliation or the
   safety checks need a test that fails before your change and passes after it.
   Say in the pull request what failure the change prevents.

## How a change gets merged

This repository is published from a private repository that also holds the
hosted platform. Pull requests are reviewed here. An accepted change is applied
in the private repository and shows up here in the next sync commit, with you as
the author. The history here only ever gets new commits added. It is never
rewritten.

## Contributor agreement

DQengine is released under the [PolyForm Shield License](LICENSE), and the same
code runs in a commercial hosted product. Contributions are accepted under the
agreement in [CLA.md](CLA.md). You keep the copyright to what you contribute, and
you give the project a licence to use it and to license it under other terms.

By opening a pull request you confirm that you have read CLA.md and agree to it.
Please say so in the pull request description.
