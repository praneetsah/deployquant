import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

ORPHAN_CHECK = "test_every_pinned_golden_was_exercised"

# How many items -k, -m or --deselect removed from this session. The orphan
# check compares the set of goldens the harness was CALLED with against the
# pinned list, so any deselection at all makes that comparison meaningless:
# the names it would report as "never replayed" were simply not selected.
#
# Counted onto the CONFIG object, and read back off `request.config`, rather
# than served as a fixture off a module global. A fixture makes the orphan
# check depend on THIS conftest being in scope for it, and that is not always
# true: collect out of this directory and back into it by explicit file
# arguments --
#
#   pytest tests/runtime/test_acceptance_tqqq_weekly.py tests/test_composer_wm74.py \
#          tests/runtime/test_golden_inventory.py
#
# -- and pytest re-enters the package, the fixture is not found and the check
# dies as `ERROR ... fixture 'deselected_count' not found`. The guard whose
# message is "that strategy is now proved by nothing" must never surface as an
# unexplained ERROR, so it must not need a fixture to do its counting. The
# config object is one per session and reaches every item.
#
# The attribute name is spelled again at test_golden_inventory.py's
# DESELECTED_ATTR, which reads it back; a test there pins the two spellings
# together, because a typo would silently leave the count at zero and turn
# `-k golden` back into a spurious failure with an alarming message.
_COUNT_ATTR = "_golden_deselected_count"


def pytest_deselected(items):
    """Count deselections onto the session's config object.

    `items` is all pytest hands this hook, so the config comes off an item;
    they all carry the same one. Registered twice (this conftest loaded under
    two module identities) it would double-count, which changes the NUMBER in
    a skip message and not the decision: zero stays zero on a full run.
    """
    if not items:
        return
    config = items[0].config
    setattr(config, _COUNT_ATTR,
            getattr(config, _COUNT_ATTR, 0) + len(items))


def pytest_collection_modifyitems(items):
    """Run the golden-orphan check last.

    It asserts on the set of names parity.assert_golden was actually called
    with, so every case has to have run before it. Today every case file is
    named test_codegen*.py and so sorts ahead of test_golden_inventory.py by
    accident of naming -- this hook is what stops that accident from being
    load-bearing. A future case file called test_weights.py or test_zz_*.py
    would collect after the inventory and fail it spuriously."""
    for item in [i for i in items if i.name == ORPHAN_CHECK]:
        items.remove(item)
        items.append(item)
