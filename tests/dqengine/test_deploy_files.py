"""The compose files a self-hoster starts from.

The env list is the part that rots: a new `os.environ.get` in the engine is
a variable somebody has to find out about, and finding out about it at 09:30
is the wrong time. So the example file has to name every one the package
reads, and this test is what keeps it naming them.
"""
import os
import re

DIST = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DEPLOY = os.path.join(DIST, "deploy")
ENV_EXAMPLE = os.path.join(DEPLOY, ".env.example")
COMPOSE = os.path.join(DEPLOY, "docker-compose.yml")

ENV_READ = re.compile(r"""os\.environ(?:\.get)?[(\[]\s*["']([A-Z][A-Z0-9_]*)["']""")

# Names the package reads but a self-hoster never sets: they are written by
# the test suite or by a container's own entry point.
NOT_THEIRS = {"SUBMIT_POOL_DEFAULT"}


def _package_env_names() -> set:
    names = set()
    for dirpath, dirnames, filenames in os.walk(os.path.join(DIST, "dqengine")):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            with open(os.path.join(dirpath, fn), encoding="utf-8") as fh:
                names |= set(ENV_READ.findall(fh.read()))
    return names - NOT_THEIRS


def _documented() -> set:
    with open(ENV_EXAMPLE, encoding="utf-8") as fh:
        return {line.split("=", 1)[0].strip() for line in fh
                if "=" in line and not line.lstrip().startswith("#")}


def test_every_variable_the_package_reads_is_in_the_example():
    missing = sorted(_package_env_names() - _documented())
    assert not missing, f"deploy/.env.example does not mention: {missing}"


def test_the_example_holds_no_values():
    with open(ENV_EXAMPLE, encoding="utf-8") as fh:
        valued = [line.strip() for line in fh
                  if "=" in line and not line.lstrip().startswith("#")
                  and line.split("=", 1)[1].strip()]
    assert not valued, f"deploy/.env.example carries values: {valued}"


def test_the_three_required_variables_are_named_first():
    text = open(ENV_EXAMPLE, encoding="utf-8").read()
    for name in ("DATABASE_URL", "REDIS_URL", "DQENGINE_SECRET"):
        assert f"\n{name}=" in text
    assert text.index("DATABASE_URL=") < text.index("PYRUN_IMAGE=")


def test_the_compose_file_restarts_the_trader_and_persists_postgres():
    text = open(COMPOSE, encoding="utf-8").read()
    assert text.count("\n    restart: unless-stopped") == 3, \
        "every service restarts; there is no supervisor of our own"
    assert "pgdata:/var/lib/postgresql/data" in text
    assert "image: postgres:16" in text and "image: redis:7.2" in text
    assert "PYRUN_ENGINE_MODE: inproc" in text
