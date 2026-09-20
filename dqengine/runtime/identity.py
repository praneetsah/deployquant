"""The identity of one resting python order — defined ONCE.

Two layers need this string and they must agree exactly:

  * the live layer turns it into the broker cid prefix
    (`broker_exec._rule_token` hashes it), and stores it on
    `BrokerOrder.rule_tag` / `Execution.rule_tag`;
  * the engine passes it to `ExecutionLedger.take(day, symbol, rule_id)`,
    which prefers rows whose `rule_tag ==` it.

If the two ever spell it differently the ledger silently stops preferring
the right rows: a fill for one order gets taken by another, or a real fill
is read as a confirmed no-fill. Both are money. So the string lives here,
in the engine (which the api may import, never the reverse), and both
sides call this function rather than formatting it themselves.

It is the ticket's own `order_id` — monotonic within an engine, assigned at
submission, unchanged for the order's life — and deliberately NOT the
order's shape plus an ordinal, which renumbered whenever an earlier
same-shaped order filled.
"""
from __future__ import annotations


# Generated code (dqengine.codegen) tags its resting orders `ir:<rule id>` so a
# block strategy compiled to python keeps the identity its IR deployment
# had. Without it, moving a blocks row to the python engine changes every cid
# prefix, the release
# pass finds none of them wanted, and cancels + resubmits every resting
# protective order at once -- into a rate limiter, with any swallowed cancel
# leaving a duplicate.
GENERATED_TAG_PREFIX = "ir:"


def intent_id(symbol: str, order_id: int, tag: str = "") -> str:
    """The identity of one resting order.

    Normally `py:{SYMBOL}:{order_id}` — the ticket's own id, stable for the
    life of the order.

    A GENERATED strategy is the exception: its tag carries the IR rule id it
    came from, and that id is the identity the IR engine already published
    for the same economic order. Honouring it is what makes the engine
    cutover a no-op at the broker.

    A user's own tag is still ignored: tags are optional and not unique, and
    the reserved `ir:` prefix is what distinguishes "generated, and this IS
    the identity" from "a human labelled their order".
    """
    if tag and str(tag).startswith(GENERATED_TAG_PREFIX):
        return str(tag)[len(GENERATED_TAG_PREFIX):]
    return f"py:{str(symbol).upper()}:{int(order_id)}"
