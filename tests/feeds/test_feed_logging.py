"""A feed's log lines reach the operator in the minute they describe.

Found on a real paper session run as `dqengine live > session.log`: the
stream connected and subscribed, `dqengine status` and the feed's own state
key both said so, and neither line was in the file. `print` block-buffers
whenever stdout is not a terminal, and a redirected file or a pipe is how a
feed is run. The lines that matter most -- a dropped socket, a refused
login, the retry interval -- are exactly the ones an operator is watching
for while they wait.
"""
import inspect
import os
import subprocess
import sys

from dqengine.feeds import alpaca, backfill
from dqengine.feeds.base import say


def _buffered_env() -> dict:
    """A child that buffers like a plain shell redirect does.
    PYTHONUNBUFFERED is set in both shipped images, so a test that
    inherited it would pass on the bug."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("PYTHONUNBUFFERED",)}
    return env


def _default_log(fn):
    return inspect.signature(fn).parameters["log"].default


def test_the_feed_and_the_backfill_default_to_the_flushing_logger():
    assert _default_log(alpaca.AlpacaQuoteFeed.__init__) is say
    assert _default_log(backfill.backfill_day) is say


def test_say_flushes_when_stdout_is_a_pipe():
    """The check that matters: not that `say` calls print, but that a line
    written through it has left the process before the next one is written."""
    code = ("import sys, time\n"
            "from dqengine.feeds.base import say\n"
            "say('[feed] connected')\n"
            "time.sleep(5)\n")           # long enough that an exit flush is no alibi
    p = subprocess.Popen([sys.executable, "-c", code],
                         stdout=subprocess.PIPE, text=True,
                         env=_buffered_env())
    try:
        line = p.stdout.readline()
    finally:
        p.kill()
        p.wait()
    assert line.strip() == "[feed] connected"


def test_a_plain_print_would_not_have():
    """The defect, pinned: the same call through `print` is what was lost."""
    code = ("import time\n"
            "print('[feed] connected')\n"
            "time.sleep(5)\n")
    p = subprocess.Popen([sys.executable, "-c", code],
                         stdout=subprocess.PIPE, text=True,
                         env=_buffered_env())
    try:
        p.stdout.flush()
        import select
        ready, _, _ = select.select([p.stdout], [], [], 1.0)
        assert not ready, ("print to a pipe reached the reader within a "
                           "second; this environment does not buffer, so "
                           "the test above proves less than it claims")
    finally:
        p.kill()
        p.wait()
