"""The open/private boundary as tests (spec 2026-09-18 §5).

Runs in two places with one file: the monorepo (where platform/api exists
and the cross-side rules apply) and the exported dqengine repo (where the
distribution root IS the repo root and only the self-contained rules apply).
A contributor cannot re-tangle the packages; neither can we.

Every path here is derived from this file's own location. Rules are never
weakened to fit the tree: there is no allowlist. (The one the bridge era
needed -- KNOWN_UNTIL_TASK_6, Rulings 10 and 12 -- went with the bridge when
the adapters crossed into dqengine.adapters.)
"""
import ast
import os
import re

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.abspath(os.path.join(HERE, ".."))          # platform/engine (or the public repo root)
MONO = os.path.abspath(os.path.join(DIST, "..", ".."))    # repo root in the monorepo
API = os.path.join(MONO, "platform", "api")
# the plugin distributions ship inside this one repo (spec 2026-09-18
# big-picture §3), so the plugin rules run in the exported repo too
PLUGINS = os.path.join(DIST, "plugins")
API_CLI_SCRIPTS = os.path.join(API, "scripts")
DIST_DEV_TOOLS = os.path.join(DIST, "tools")
IN_MONOREPO = os.path.isdir(API)

# Where the distribution lives in the monorepo (oss/EXPORT.yml): failure
# messages name files by monorepo-relative path in both places the guard runs.
DIST_IN_MONO = "platform/engine"

OPEN_TOPS = ("dqengine",)
RUNTIME = os.path.join(DIST, "dqengine", "runtime")
SKIP_DIRS = ("__pycache__", ".venv", "node_modules", ".git", "build", "dist")


def _key(path):
    """Monorepo-relative, forward-slash key for `path`, the same in both
    places the guard runs."""
    if IN_MONOREPO:
        rel = os.path.relpath(path, MONO)
    else:
        rel = os.path.join(DIST_IN_MONO, os.path.relpath(path, DIST))
    return rel.replace(os.sep, "/")


def _py_files(root, skip_tests=True):
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns
                  if d not in SKIP_DIRS and not d.endswith(".egg-info")
                  and not (skip_tests and d == "tests")]
        for fn in fns:
            if fn.endswith(".py"):
                yield os.path.join(dp, fn)


def _open_files():
    """Every module the distribution ships."""
    for top in OPEN_TOPS:
        pkg, mod = os.path.join(DIST, top), os.path.join(DIST, top + ".py")
        if os.path.isdir(pkg):
            yield from _py_files(pkg)
        elif os.path.isfile(mod):
            yield mod


def _imports(path):
    """Top-level names of every absolute `import x` / `from x import y`."""
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), path)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                yield a.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            yield node.module.split(".")[0]


def _module_name(path):
    """Dotted module name of a file under DIST (`dqengine/runtime/core/data.py`
    -> `dqengine.runtime.core.data`; a package's __init__ is the package)."""
    parts = os.path.relpath(path, DIST)[:-3].split(os.sep)
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def _full_imports(path):
    """Full dotted module of every import, relative ones resolved against
    the file's own package -- `from ..live import bus` inside
    dqengine/runtime/x.py is `dqengine.live`, and the rule must see it."""
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), path)
    pkg = _module_name(path).split(".")
    if os.path.basename(path) != "__init__.py":
        pkg = pkg[:-1]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                yield a.name
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                base = node.module or ""
            else:
                up = pkg[:len(pkg) - (node.level - 1)]
                base = ".".join(up + ([node.module] if node.module else []))
            yield base
            for a in node.names:            # `from dqengine import live`
                yield f"{base}.{a.name}"


def _outside_runtime(name):
    return (name == "dqengine" or name.startswith("dqengine.")) and not (
        name == "dqengine.runtime" or name.startswith("dqengine.runtime."))


def _api_module_names():
    """Every importable name platform/api offers: its top-level modules and
    its packages (directories with an __init__.py). Empty outside the monorepo."""
    if not IN_MONOREPO:
        return set()
    names = {fn[:-3] for fn in os.listdir(API) if fn.endswith(".py")}
    names |= {d for d in os.listdir(API)
              if os.path.isfile(os.path.join(API, d, "__init__.py"))}
    return names - {"tests"}


def _is_test_code(path):
    """Test code may put its own package dir on sys.path (Ruling 8): any
    conftest.py, and any module under a directory named `tests`."""
    parts = os.path.relpath(path, MONO if IN_MONOREPO else DIST).split(os.sep)
    return parts[-1] == "conftest.py" or "tests" in parts[:-1]


def _is_cli_script(path):
    """Standalone scripts run as `python <dir>/x.py`, exempt from R5: the
    private operator tools under platform/api/scripts/ (spec R5) and the
    distribution's dev CLIs under tools/ (Task 5 ruling (a): pyproject
    excludes `tools*` from the installed package, so they are no more
    importable code than scripts/ is)."""
    return path.startswith(DIST_DEV_TOOLS + os.sep) or (
        IN_MONOREPO and path.startswith(API_CLI_SCRIPTS + os.sep))


# ---- R1/R2/R4: open packages never import the private api ------------------

@pytest.mark.skipif(not IN_MONOREPO, reason="cross-side rule; monorepo only")
def test_open_code_never_imports_a_private_api_module():
    private = _api_module_names()
    bad = []
    for f in _open_files():
        hits = sorted(set(_imports(f)) & private)
        if hits:
            bad.append((_key(f), hits))
    assert not bad, f"open code imports private api modules: {bad}"


def test_runtime_never_imports_the_rest_of_dqengine_or_a_plugin():
    """R1: the engine proper (dqengine.runtime) sits below everything else in
    the package -- feeds, adapters, live stack, sandbox, codegen -- and below
    every plugin. It imports itself and third-party packages only. (`from
    dqengine.runtime import x` also yields the candidate name
    `dqengine.runtime.x`, which is inside; a bare `dqengine` base is the
    parent of an inside name and is not a hit on its own.)"""
    bad = []
    for f in _py_files(RUNTIME):
        hits = sorted(n for n in set(_full_imports(f))
                      if n.startswith("dqengine_")
                      or (_outside_runtime(n) and n != "dqengine"))
        if hits:
            bad.append((os.path.relpath(f, DIST), hits))
    assert not bad, f"dqengine.runtime must sit below the rest of dqengine: {bad}"


def test_dqengine_never_imports_a_plugin():
    """R2: the engine never depends on a plugin. Plugins are discovered
    through the `dqengine.brokers` entry-point group, never imported by
    name -- `dqengine_webull` and friends must not appear in dqengine/."""
    bad = []
    for f in _py_files(os.path.join(DIST, "dqengine")):
        hits = sorted(n for n in set(_imports(f)) if n.startswith("dqengine_"))
        if hits:
            bad.append((os.path.relpath(f, DIST), hits))
    assert not bad, f"dqengine imports a plugin by name: {bad}"


@pytest.mark.skipif(not IN_MONOREPO, reason="cross-side rule; monorepo only")
def test_exported_tests_never_import_private_api_modules():
    """A test that ships with a distribution must collect in the public
    repo, where platform/api does not exist: no test under DIST/tests (or
    under a plugin's tests/) may import an api module by name."""
    private = _api_module_names()
    roots = [os.path.join(DIST, "tests")]
    if os.path.isdir(PLUGINS):
        roots += [os.path.join(PLUGINS, p, "tests") for p in os.listdir(PLUGINS)]
    bad = []
    for root in roots:
        if not os.path.isdir(root):
            continue
        for f in _py_files(root, skip_tests=False):
            hits = sorted(set(_imports(f)) & private)
            if hits:
                bad.append((_key(f), hits))
    assert not bad, f"exported tests import private api modules: {bad}"


# ---- R3: plugins import only dqengine + their vendor SDK -------------------

@pytest.mark.skipif(not os.path.isdir(PLUGINS), reason="no plugins dir here")
def test_plugins_import_only_dqengine_and_their_sdk():
    private = _api_module_names()
    bad = []
    for plugin in os.listdir(PLUGINS):
        for f in _py_files(os.path.join(PLUGINS, plugin)):
            names = set(_imports(f))
            if names & private:
                bad.append((_key(f), sorted(names & private)))
            if any(n == "dqengine.runtime" or n.startswith("dqengine.runtime.")
                   for n in _full_imports(f)):
                bad.append((_key(f), ["dqengine.runtime (go through dqengine's adapter surface)"]))
    assert not bad, f"plugin imports outside its lane: {bad}"


# ---- R5: no sys.path mutation in importable code ----------------------------

# every way to grow sys.path: .insert( / .append( / .extend(, slice or
# index assignment (sys.path[:0] = ..., sys.path[0:0] = ...; `=(?!=)` so a
# comparison like `sys.path[0] == x` is not a mutation), and +=
SYS_PATH_MUTATION = re.compile(
    r"sys\.path\s*(\.(insert|append|extend)\s*\(|\[[^\]]*\]\s*=(?!=)|\+=)")


def test_no_sys_path_mutation_outside_test_code_and_cli_scripts():
    roots = [DIST] + ([API] if IN_MONOREPO else [])     # DIST holds plugins/
    bad = []
    for root in roots:
        if not os.path.isdir(root):
            continue
        for f in _py_files(root, skip_tests=False):
            if _is_test_code(f) or _is_cli_script(f):
                continue
            with open(f, encoding="utf-8") as fh:
                if SYS_PATH_MUTATION.search(fh.read()):
                    bad.append(_key(f))
    assert not bad, f"sys.path mutation in importable code: {sorted(bad)}"


# ---- R7: hygiene files on every exported directory --------------------------

def _export_entries():
    """The `path:` of every oss/EXPORT.yml entry, absolute; just this
    distribution when the allowlist is not here (the exported repo). A tiny
    fixed-shape parser: the engine takes no yaml dependency."""
    path = os.path.join(MONO, "oss", "EXPORT.yml")
    if not os.path.isfile(path):
        return [DIST]
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    entries = [os.path.join(MONO, *m.group(1).split("/"))
               for m in re.finditer(r"^\s*-?\s*path:\s*(\S+)", text, re.M)]
    assert entries, f"{path} exists but no `path:` entries parsed -- shape changed?"
    return entries


def test_every_exported_directory_carries_license_notice_readme():
    missing = []
    # an entry whose directory is not there yet (the plugins until Task 7) is
    # the exporter's "directory missing -- skipped", not this rule's business
    # each plugin is its own pip distribution inside the one exported repo,
    # so it carries the three files for its own sdist
    dirs = [d for d in _export_entries() if os.path.isdir(d)]
    if os.path.isdir(PLUGINS):
        dirs += [os.path.join(PLUGINS, p) for p in sorted(os.listdir(PLUGINS))
                 if os.path.isfile(os.path.join(PLUGINS, p, "pyproject.toml"))]
    for d in dirs:
        for name in ("LICENSE", "NOTICE", "README.md"):
            if not os.path.isfile(os.path.join(d, name)):
                missing.append(_key(os.path.join(d, name)))
    assert not missing, missing


# ---- R9: one code copy -- no module name on both sides ----------------------

@pytest.mark.skipif(not IN_MONOREPO, reason="cross-side rule; monorepo only")
def test_no_module_exists_on_both_sides_of_the_line():
    """"A module name exists on both sides" means the api's TOP-LEVEL names
    (its `*.py` and its packages), deliberately: a same-named module inside
    an api sub-package is that package's business, and every move deletes
    its source (spec R9) -- the guard catches the shim left behind."""
    open_names = set()
    for top in ("dqengine",):
        for f in _py_files(os.path.join(DIST, top)):
            open_names.add(os.path.splitext(os.path.basename(f))[0])
    open_names -= {"__init__"}
    dup = sorted(open_names & _api_module_names())
    assert not dup, f"module exists on both sides (a shim or a stale copy): {dup}"
