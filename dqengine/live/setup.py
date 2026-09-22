"""The two rows one live deployment runs on, and what it refuses to create.

A hosted platform creates these rows behind a user account and a strategy
library. A self-hosted install has neither, so this module is the same step
without them: one broker connection and one deployment, from an algorithm
file on disk.

The refusals are here rather than in the command line because they are
decisions about what may trade, and both callers have to give the same
answer. A deploy endpoint calls `check_resolution` and
`check_one_deployment` the way `prepare` does, and there is one rule instead
of two that have to be kept in step.

Re-running `prepare` with the same algorithm file and the same broker
updates the rows it made the first time. It never creates a second
deployment on a connection: the combiner that folds several strategies onto
one broker account is not part of this engine, so the executor refuses a
connection carrying more than one, and so does this.
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# Credentials as JSON, for a broker whose adapter reads none from the
# environment itself. The keys are whatever that adapter's own README
# documents; nothing here interprets them.
CREDS_ENV = "DQENGINE_BROKER_CREDS"

# The executor's own names for the two notional caps (see `rails_from`).
CAP_ORDER_KEY = "max_order_notional"
CAP_POSITION_KEY = "max_position_notional"


class DeploymentRefused(RuntimeError):
    """This algorithm, this account or this combination may not run live.
    Always actionable: the message says what to change."""


SECOND_REFUSAL = (
    "second-resolution live deployments aren't available yet — "
    "second-resolution backtests are available today.")

LIVE_REFUSAL = (
    "--live needs this broker connection's execution truth to be `enforce`, "
    "and it is {state}. `dqengine adopt` is the step that gets there: it "
    "pulls the account's own trade history into the ledger, prints what the "
    "broker holds next to what this strategy's replay holds, and sets "
    "enforce once you type the account label back. Run it on the same "
    "algorithm and broker as this command. Until then, run paper, or run "
    "--dry-run against the real account: --dry-run computes the real orders "
    "and sends nothing.")

ONE_DEPLOYMENT_REFUSAL = (
    "connection {conn_id}: {name!r} ({dep_id}) is already running or paused "
    "on this broker connection. The open engine trades one deployment per "
    "connection: stop or move the other one, or point this algorithm at a "
    "second broker connection.")

DAILY_ORDER_TYPE_REFUSAL = (
    "{broker} can't place {types} orders that this daily strategy uses — "
    "{note}")


class _AsEnforcing:
    """The connection as it will be a moment from now.

    `dqengine adopt` creates these rows and then raises the connection to
    `enforce`, so the daily question has to be asked of the mode it is about
    to have. Every OTHER reason `daily_broker_refusal` can give -- a
    connection that is gone, a venue with no adapter in this install --
    still fires, which is why this is a view of the row rather than a flag
    that skips the call."""
    execution_truth = "enforce"

    def __init__(self, conn):
        self._conn = conn

    def __getattr__(self, name):
        return getattr(self._conn, name)


# --------------------------------------------------------------- the checks

def check_resolution(resolution: str) -> None:
    """Refuse a bar resolution the live path cannot serve. Daily and minute
    both run; second does not yet."""
    if resolution == "second":
        raise DeploymentRefused(SECOND_REFUSAL)


def check_daily(conn, code: str, broker: str, *,
                require_enforce: bool = True) -> None:
    """What a DAILY deployment needs from the account it is pointed at.

    A daily strategy's orders fill at this session's close and the next
    session's open, which is where its backtest fills them, and the live
    payload publishes each one at the moment the strategy meant it. Two
    things about the destination decide whether that can happen, and both
    are asked here so the answer is the one the tick would give:

    * `daily_broker_refusal` -- the driver's own question, asked again at
      every tick. The connection has to be enforcing: the at-close order
      goes out a minute before the close and the model settles the ticket a
      minute after it, and in between only the broker's own execution rows
      say whether it went out.
    * whether the venue can take or emulate the at-close order the strategy
      will place. On daily data a market order placed while the session is
      open BECOMES one, which a scan of the source cannot see, so the scan
      is told the resolution.

    `require_enforce=False` is for the one caller whose next step is to set
    `enforce`: `dqengine adopt` creates these rows and then raises the
    connection, so refusing here on a mode the command is about to change
    would make a daily strategy impossible to adopt at all. The venue
    question is still asked, and the tick asks `daily_broker_refusal` again
    every time it fires, so a daily deployment left below `enforce` still
    does not trade.
    """
    from dqengine.adapters import catalog
    from dqengine.live import capabilities
    from dqengine.live.driver.deployment import daily_broker_refusal
    asked = conn if (require_enforce or conn is None) else _AsEnforcing(conn)
    reason = daily_broker_refusal(asked)
    if reason:
        raise DeploymentRefused(reason)
    bad = capabilities.unsupported_for_deploy(
        catalog.get_adapter(broker).caps,
        capabilities.python_order_types(code, daily=True),
        kind="python", daily=True)
    if bad:
        raise DeploymentRefused(DAILY_ORDER_TYPE_REFUSAL.format(
            broker=broker, types=", ".join(r.order_type for r in bad),
            note=bad[0].note))


def check_one_deployment(session, conn_id: str, name: str) -> object:
    """The deployment this connection already carries, if it is this one.

    Returns the row to update, or None when the connection is free. Raises
    when something else is on it -- the same rule the executor applies at
    sweep time, applied here where the message can still be acted on."""
    from dqengine.live.persistence import managed_deployments
    rows = managed_deployments(session, conn_id)
    mine = [d for d in rows if d.name == name]
    others = [d for d in rows if d.name != name]
    if others:
        d = others[0]
        raise DeploymentRefused(ONE_DEPLOYMENT_REFUSAL.format(
            conn_id=conn_id, name=d.name, dep_id=d.id))
    if len(mine) > 1:
        raise DeploymentRefused(ONE_DEPLOYMENT_REFUSAL.format(
            conn_id=conn_id, name=mine[1].name, dep_id=mine[1].id))
    return mine[0] if mine else None


def require_enforce(conn) -> None:
    """Real money needs the connection in `enforce`: broker fills, not the
    model's, drive the accounting. Nothing else is allowed to send."""
    truth = getattr(conn, "execution_truth", None) if conn is not None else None
    if truth == "enforce":
        return
    state = (f"`{truth}`" if truth else
             "not set yet (this broker has no connection here)")
    raise DeploymentRefused(LIVE_REFUSAL.format(state=state))


# ------------------------------------------------------------ the algorithm

def strategy_manifest(code: str, start: date, end: date, cash: float) -> dict:
    """{subscriptions, resolution, start, end, cash, warmup_days} for one
    algorithm: what it subscribes to and at what resolution.

    The same pass the hosted deploy endpoint runs, through the same engine
    mode this process ticks in. Under `sandbox` the algorithm runs in a
    container, as it does on the hosted platform; under `inproc` -- what a
    self-hoster running their own code gets -- it runs here, which is the
    whole meaning of that mode."""
    from dqengine.sandbox import pyrunner
    cfg = {"mode": "manifest", "start": start.isoformat(),
           "end": end.isoformat(), "cash": float(cash)}
    if pyrunner.ENGINE_MODE == "inproc":
        from dqengine.config import DATA_ROOT
        from dqengine.runtime import run_python_backtest
        res = run_python_backtest(code, DATA_ROOT, overrides={
            "start": cfg["start"], "end": cfg["end"], "cash": cfg["cash"]},
            manifest_only=True)
    else:
        res = pyrunner.run(code, cfg, timeout_s=60)
    if "error" in res:
        err = res["error"]
        raise DeploymentRefused(
            f"could not analyze this algorithm: "
            f"{err.get('type', 'Error')}: {err.get('message', '')}")
    man = res["manifest"]
    if not man.get("subscriptions"):
        raise DeploymentRefused(
            "this algorithm subscribes to no symbols — call add_equity in "
            "initialize")
    return man


def screen_determinism(code: str) -> None:
    """A live strategy must be a pure function of its bars: every tick
    replays its whole history and the executor reads any disagreement as a
    position change to trade on."""
    from dqengine.live import determinism
    problems = determinism.screen(code)
    if problems:
        raise DeploymentRefused(
            "this algorithm isn't deterministic, so it can't run live — "
            "every tick replays its whole history and the results have to "
            "match. Found: " + "; ".join(problems[:4]))


# ------------------------------------------------------------ the two rows

def find_connection(session, broker: str):
    """The connection this install uses for a broker: the oldest one, so a
    re-run finds what the last run made. One connection per broker is what
    the command line creates; a host with more passes its own id."""
    from dqengine.live.persistence import BrokerConnection
    rows = (session.query(BrokerConnection)
            .filter(BrokerConnection.broker == broker).all())
    return min(rows, key=lambda r: (r.created_at is None, r.created_at, r.id),
               default=None)


def creds_from_env(broker: str, env=None) -> dict:
    """The broker's credentials, out of the environment.

    Two ways in, and no third: the adapter's own `creds_from_env`, which is
    the variable names its README documents, or `DQENGINE_BROKER_CREDS` as
    a JSON object for an adapter that reads none. The JSON wins when both
    are present -- it is the more explicit of the two."""
    env = os.environ if env is None else env
    raw = (env.get(CREDS_ENV) or "").strip()
    if raw:
        try:
            creds = json.loads(raw)
        except ValueError as e:
            raise DeploymentRefused(
                f"{CREDS_ENV} is not valid JSON: {e}") from None
        if not isinstance(creds, dict) or not creds:
            raise DeploymentRefused(
                f"{CREDS_ENV} must be a JSON object of credential fields")
        return creds
    from dqengine.adapters import catalog
    try:
        cls = catalog.adapter_class(broker)
    except Exception as e:                                  # noqa: BLE001
        raise DeploymentRefused(f"{broker}: {e}") from None
    hook = getattr(cls, "creds_from_env", None)
    if hook is not None:
        try:
            return hook(env)
        except KeyError as e:
            raise DeploymentRefused(
                f"{broker}: {e.args[0] if e.args else e}") from None
    raise DeploymentRefused(
        f"no credentials for {broker}: its adapter reads none from the "
        f"environment, so pass them as a JSON object in {CREDS_ENV}. The "
        f"field names are the ones that adapter's README documents.")


def ensure_vault_secret(session, create: bool = True) -> str | None:
    """The key broker credentials are encrypted under. Returns the path of a
    key file this call wrote, or None when one was already available.

    The vault never invents a key, and it is right not to: on a machine that
    already holds encrypted rows a fresh key is indistinguishable from the
    correct one until every connection fails to decrypt. This function is
    the one place that can tell it is a first run -- the connections table
    is empty -- and it says what it wrote and where."""
    import secrets
    import stat

    from dqengine.live import vault
    from dqengine.live.persistence import BrokerConnection
    if os.environ.get(vault.SECRET_ENV) or os.path.exists(vault.SECRET_PATH):
        return None
    held = session.query(BrokerConnection).count()
    if held:
        raise DeploymentRefused(
            f"this database already holds {held} encrypted broker "
            f"connection(s) and no credential secret is available. Set "
            f"{vault.SECRET_ENV} to the secret those rows were written "
            f"under. A new key would not decrypt them.")
    if not create:
        raise DeploymentRefused(
            f"no credential secret: set {vault.SECRET_ENV}, or write one to "
            f"{vault.SECRET_PATH}")
    path = vault.SECRET_PATH
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                 stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w") as fh:
        fh.write(secrets.token_urlsafe(48))
    return path


def caps_settings(max_order_usd=None, max_position_usd=None,
                  dry_run: bool = False) -> dict:
    """The connection settings the two cap flags and --dry-run write.

    Caps are off unless a number is given: a cap the engine picked would
    bind arbitrarily on an account whose size it does not know. The keys are
    the executor's own (`rails_from`), so nothing translates between what is
    stored and what the rails read."""
    out: dict = {"dry_run": bool(dry_run)}
    if max_order_usd is not None:
        out[CAP_ORDER_KEY] = float(max_order_usd)
    if max_position_usd is not None:
        out[CAP_POSITION_KEY] = float(max_position_usd)
    return out


def _merge_settings(existing: dict | None, wanted: dict) -> dict:
    """Flags own their own keys and nothing else: a cap cleared on the
    command line is cleared in the row, and a setting someone wrote by hand
    (a price band, a pause) survives the run."""
    out = dict(existing or {})
    for key in (CAP_ORDER_KEY, CAP_POSITION_KEY, "dry_run"):
        out.pop(key, None)
    out.update(wanted)
    return out


def upsert_connection(session, broker: str, creds: dict, *, live: bool,
                      settings: dict, existing=None):
    """One broker connection, created or refreshed. Returns (row, created).

    `execution_truth` is set only on creation -- `observe` for paper, which
    polls the broker's fills and shows them without letting them drive the
    accounting. Raising an existing connection to `enforce` is a separate,
    deliberate step and this never does it silently."""
    from dqengine.live.persistence import BrokerConnection
    from dqengine.live.vault import encrypt_creds
    mode = "live" if live else "paper"
    row = existing
    created = row is None
    if created:
        row = BrokerConnection(broker=broker, execution_truth="observe")
        session.add(row)
    row.label = row.label or f"{broker} (dqengine live)"
    row.mode = mode
    row.status = "connected"
    row.creds_encrypted = encrypt_creds(creds)
    row.settings = _merge_settings(row.settings, settings)
    session.flush()
    return row, created


def upsert_deployment(session, conn, *, name: str, code: str, universe: list,
                      resolution: str, start_date: date, cash: float,
                      margin: float, live: bool, existing=None):
    """One deployment, created or refreshed. Returns (row, created).

    A re-run replaces the code snapshot and what the snapshot subscribes to.
    It does not move `start_date`, `cash_initial` or `margin_max` unless the
    caller asked: those three define the replay, and moving them silently
    would change what the strategy is holding, which the executor reads as
    a position to trade to."""
    from dqengine.live.persistence import Deployment
    row = existing
    created = row is None
    if created:
        row = Deployment(name=name, kind="python", cash_initial=float(cash),
                         margin_max=float(margin), start_date=start_date,
                         broker_connection_id=conn.id)
        session.add(row)
    row.kind = "python"
    row.code = code
    row.ir = None
    row.universe = [s.upper() for s in universe]
    row.resolution = resolution
    row.status = "running"
    row.paused_at = None
    row.mode = "live" if live else "paper"
    row.broker_connection_id = conn.id
    row.live_confirmed = bool(live)
    if not created:
        if cash is not None:
            row.cash_initial = float(cash)
        if margin is not None:
            row.margin_max = float(margin)
        if start_date is not None:
            row.start_date = start_date
    session.flush()
    return row, created


def default_start(now_et: datetime | None = None) -> date:
    """Where a replay starts when the caller names no day: today before the
    open, tomorrow after it. A start in the middle of a session would make
    the replay miss the morning it is about to trade against."""
    now_et = now_et or datetime.now(ET)
    if (now_et.hour, now_et.minute) < (9, 30):
        return now_et.date()
    return now_et.date() + timedelta(days=1)


def prepare(algorithm: str, broker: str, *, live: bool = False,
            dry_run: bool = False, cash: float | None = None,
            start: date | None = None, margin: float | None = None,
            max_order_usd=None, max_position_usd=None,
            creds: dict | None = None, name: str | None = None,
            create_secret: bool = True, env=None, init: bool = True,
            adopting: bool = False) -> dict:
    """Everything one deployment needs in the database, from an algorithm
    file and a broker id. Idempotent: the same file and broker find the same
    two rows again.

    Order is not arbitrary. Every refusal is made before the commit -- the
    file, the live gate, the credentials, the resolution, what a daily
    strategy needs from its connection, the one-deployment rule -- so a run
    that is going to be refused leaves the database exactly as it found it.

    `init=False` skips the schema step for a caller whose database is
    already built (a test, a host that runs its own migrations).

    `adopting=True` is `dqengine adopt`, whose next step is to raise the
    connection to `enforce`: a daily strategy's enforce requirement is
    asked of the mode the connection is about to have, so the one command
    that gets an account there is not refused by the state it is there to
    change. See `check_daily`."""
    from dqengine.live.persistence import SessionLocal, init_db
    try:
        with open(algorithm, encoding="utf-8") as fh:
            code = fh.read()
    except OSError as e:
        raise DeploymentRefused(f"{algorithm}: {e.strerror}") from None
    name = name or os.path.splitext(os.path.basename(algorithm))[0]
    if init:
        init_db()
    out: dict = {"name": name, "broker": broker, "secret_path": None}
    with SessionLocal() as session:
        conn = find_connection(session, broker)
        if live:
            require_enforce(conn)
        out["secret_path"] = ensure_vault_secret(session, create=create_secret)
        if creds is None:
            creds = creds_from_env(broker, env)
        screen_determinism(code)
        start_date = start or default_start()
        man = strategy_manifest(code, start_date, date.today(),
                                cash if cash is not None else 1000.0)
        resolution = man.get("resolution") or "minute"
        check_resolution(resolution)
        settings = caps_settings(max_order_usd, max_position_usd, dry_run)
        conn, conn_created = upsert_connection(
            session, broker, creds, live=live, settings=settings,
            existing=conn)
        # After the connection exists, because both of its questions are
        # about the connection. Nothing is committed until the end of this
        # block, so a refusal here still leaves the database as it was.
        if resolution == "daily":
            check_daily(conn, code, broker,
                        require_enforce=not adopting)
        dep_existing = check_one_deployment(session, conn.id, name)
        if dep_existing is None:
            dep_cash = 1000.0 if cash is None else cash
            dep_margin = 1.0 if margin is None else margin
            dep_start = start_date
        else:
            # a re-run only moves the replay's three defining numbers when
            # the caller named them
            dep_cash, dep_margin = cash, margin
            dep_start = start
        dep, dep_created = upsert_deployment(
            session, conn, name=name, code=code,
            universe=man["subscriptions"], resolution=resolution,
            start_date=dep_start, cash=dep_cash, margin=dep_margin,
            live=live, existing=dep_existing)
        out.update(conn_id=conn.id, dep_id=dep.id,
                   connection_created=conn_created,
                   deployment_created=dep_created,
                   universe=list(dep.universe), resolution=dep.resolution,
                   start_date=dep.start_date, cash=dep.cash_initial,
                   margin=dep.margin_max, mode=conn.mode,
                   execution_truth=conn.execution_truth,
                   settings=dict(conn.settings or {}))
        session.commit()
    return out
