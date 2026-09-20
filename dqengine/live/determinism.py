"""A deployed python strategy must be a pure function of its bar data.

The live model is not signal generation, it is state RECONCILIATION: every
tick re-simulates the whole history and the final state is the truth. So a
strategy that disagrees with itself between ticks does not merely report
oddly — the executor reads the disagreement as a position change and TRADES
on it.

Two defences, because neither is sufficient alone:

  screen()               refuses the obvious sources at deploy time, where
                         the message can still be acted on
  history_fingerprint()  catches everything else at runtime, by noticing
                         that settled history changed under us

The screen is deliberately not clever. It names what it found and points at
the line; it is a guard rail, not a proof, and anything it cannot see is
caught by the fingerprint.
"""
import ast
import hashlib

# Modules that make a run depend on something other than its bars. The
# sandbox already has no network, so an import of these is a bug the user
# wants told about at deploy rather than a mystery failure at runtime.
_BANNED_MODULES = {
    "random": "uses random without a fixed seed",
    "socket": "opens network connections",
    "urllib": "fetches over the network",
    "urllib2": "fetches over the network",
    "requests": "fetches over the network",
    "http": "fetches over the network",
    "aiohttp": "fetches over the network",
    "secrets": "generates unpredictable values",
    "uuid": "generates unpredictable values",
}

# Wall-clock reads. self.time is the SIMULATION clock and is always fine;
# these are the real one, which differs on every tick.
_BANNED_CALLS = {
    ("datetime", "now"): "reads the wall clock (use self.time)",
    ("datetime", "today"): "reads the wall clock (use self.time)",
    ("date", "today"): "reads the wall clock (use self.time)",
    ("time", "time"): "reads the wall clock (use self.time)",
    ("time", "monotonic"): "reads the wall clock (use self.time)",
}


def _seeded(tree: ast.AST) -> bool:
    """random.seed(...) / Random(n) anywhere makes randomness reproducible."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Attribute) and f.attr == "seed":
                return True
            if isinstance(f, ast.Name) and f.id == "Random" and node.args:
                return True
    return False


def screen(code: str) -> list:
    """Reasons this code may not be deterministic. Empty means it passed the
    things we can see statically — never that it is proven pure."""
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return [f"line {e.lineno}: the code does not parse ({e.msg})"]

    seeded = _seeded(tree)
    out, seen = [], set()

    def add(line, why):
        key = (line, why)
        if key not in seen:
            seen.add(key)
            out.append(f"line {line}: {why}")

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                root = a.name.split(".")[0]
                if root in _BANNED_MODULES:
                    if root == "random" and seeded:
                        continue
                    add(node.lineno, _BANNED_MODULES[root])
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root in _BANNED_MODULES:
                if root == "random" and seeded:
                    continue
                add(node.lineno, _BANNED_MODULES[root])
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            f = node.func
            owner = getattr(f.value, "id", None) or getattr(
                getattr(f.value, "attr", None), "__str__", lambda: None)()
            if (owner, f.attr) in _BANNED_CALLS:
                add(node.lineno, _BANNED_CALLS[(owner, f.attr)])
    return out


def history_fingerprint(equity_days: list, equity: list, today) -> str:
    """A hash of the SETTLED equity curve — every session strictly before
    `today`. Yesterday's history can never legitimately change; if this moves
    between ticks, the strategy is not reproducing itself and must not be
    allowed to trade on the difference.

    Today is excluded on purpose: the live session is still moving, and its
    equity changing tick to tick is the system working.
    """
    iso = today.isoformat() if hasattr(today, "isoformat") else str(today)
    h = hashlib.sha256()
    for d, v in zip(equity_days, equity):
        ds = d if isinstance(d, str) else d.isoformat()
        if ds >= iso:
            break
        h.update(f"{ds}:{round(float(v), 4)}|".encode())
    return h.hexdigest()[:32]
