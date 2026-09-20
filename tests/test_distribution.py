"""The open distribution must be installable and complete (spec R7): the
three hygiene files exist, the NOTICE credits LEAN, and the packages the
pyproject discovers are exactly the open ones."""
import json
import os
import re
import subprocess
import sys
from importlib.metadata import distributions, entry_points

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))          # platform/engine


def _read(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as fh:
        return fh.read()


def _installed_editable():
    """True when the `dqengine` distribution this interpreter sees was
    installed with `pip install -e` (PEP 610: direct_url.json carries
    `"dir_info": {"editable": true}`). False for a plain `pip install .`,
    a wheel, or no install at all.

    Every distribution of that name is consulted, not just the first: the
    editable build leaves a `dqengine.egg-info` in the source tree, and
    with ROOT on sys.path (pytest's rootdir) that shadow -- which carries
    no direct_url.json -- is what `distribution()` would hand back."""
    for d in distributions(name="deployquant"):
        raw = d.read_text("direct_url.json")
        if raw and json.loads(raw).get("dir_info", {}).get("editable"):
            return True
    return False


def test_hygiene_files_exist_and_notice_credits_lean():
    for name in ("LICENSE", "NOTICE", "README.md"):
        assert os.path.isfile(os.path.join(ROOT, name)), name
    lic = _read("LICENSE")
    assert "PolyForm Shield License 1.0.0" in lic and "## Noncompete" in lic
    # Shield carries the copyright in a `Required Notice:` line, not in the text
    assert "Required Notice: Copyright" in _read("NOTICE")
    # the LEAN-derived portions stay Apache-2.0; s4(a) wants a copy shipped
    apache = _read(os.path.join("LICENSES", "Apache-2.0.txt"))
    assert "Apache License" in apache and "Version 2.0" in apache
    notice = _read("NOTICE")
    assert "QuantConnect" in notice and "LEAN" in notice and "Apache" in notice
    # Apache-2.0 s4(c): a derivative work retains the upstream copyright line.
    assert "Copyright 2014-present QuantConnect Corporation" in notice


def test_readme_is_user_facing_copy():
    """User-facing copy may say LEAN-compatible; it never names QuantConnect
    (the NOTICE is the legal document and does) and never names the
    hosted platform's data vendor."""
    readme = _read("README.md")
    assert "LEAN-compatible" in readme
    assert "QuantConnect" not in readme
    # Schwab is a broker plugin here and may be named as one; what the copy
    # must never do is present it as a market-data source
    for line in readme.splitlines():
        if "schwab" in line.lower():
            assert not re.search(r"\b(data|feed|bars|quotes?)\b", line, re.I), line


def test_pyproject_discovers_open_packages_by_pattern():
    """Package discovery is by pattern (dqengine*), so adding
    dqengine.live or dqengine.sandbox later needs no pyproject edit."""
    text = _read("pyproject.toml")
    assert "[tool.setuptools.packages.find]" in text
    assert re.search(r'include\s*=\s*\[\s*"dqengine\*"\s*\]', text)
    assert 'sandbox = [' in text, "the sandbox service's fastapi/uvicorn are an extra"


def test_discovery_and_entry_points_are_real_not_textual():
    """The pyproject's patterns, fed to setuptools itself, find at least the
    four open packages (>=: later tasks add dqengine.live, dqengine.sandbox),
    and the installed metadata registers the bundled broker adapter."""
    # only this test drives setuptools directly; a contributor without it
    # (PEP 517 builds it in an isolated env) skips here, not at collection
    find_packages = pytest.importorskip("setuptools").find_packages
    found = set(find_packages(
        where=ROOT,
        include=["dqengine*"],
        exclude=["tests*", "strategies*", "tools*"]))
    assert found >= {"dqengine.runtime", "dqengine.runtime.core", "dqengine", "dqengine.adapters"}, found
    assert not {p for p in found if p.split(".")[0] in ("tests", "strategies", "tools")}, found
    brokers = {ep.name for ep in entry_points(group="dqengine.brokers")}
    assert {"alpaca", "alpaca-paper"} <= brokers, brokers


def test_distribution_is_importable_from_anywhere():
    """The three top-level modules import from a cwd that is NOT the engine
    directory, with no sys.path help -- the property every later task
    relies on. When the install is editable (`pip install -e`, Task 1 step
    4) each module must also resolve to ITS file in THIS tree, not a stale
    site-packages copy or a dangling editable mapping; a plain
    `pip install .` (a public contributor) legitimately imports from
    site-packages, so that half is skipped rather than failed."""
    out = subprocess.run(
        [sys.executable, "-c",
         "import dqengine.runtime, dqengine, dqengine.codegen; "
         "print(dqengine.runtime.__file__); print(dqengine.__file__); print(dqengine.codegen.__file__)"],
        cwd="/", capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    paths = out.stdout.strip().splitlines()
    assert len(paths) == 3, out.stdout
    if not _installed_editable():
        pytest.skip("dqengine is not an editable install (pip install -e); "
                    "the modules import fine but come from site-packages, not this tree")
    expected = [("dqengine", "runtime", "__init__.py"), ("dqengine", "__init__.py"), ("dqengine", "codegen.py")]
    for p, parts in zip(paths, expected):
        want = os.path.realpath(os.path.join(ROOT, *parts))
        assert os.path.realpath(p) == want, (p, want)
