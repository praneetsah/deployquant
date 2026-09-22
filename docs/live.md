# Live trading

`dqengine live` runs one algorithm against one broker account. It replays
the algorithm from its start date on every bar, compares what the replay
holds against what the broker holds, and sends the difference.

This is the same code the DeployQuant platform runs. The command line
creates two database rows and starts four things in one process: the market
data feed, the two bus consumers, the order executor and the tick loop.

Paper trading needs one command. Real money needs two: `dqengine adopt`
first, then `dqengine live --live`. See [Real money](#real-money).

## What you need

- Postgres and Redis. `deploy/docker-compose.yml` starts both.
- Alpaca market data keys. A free account is enough for history and for the
  IEX tape. Full market data on the live stream needs Alpaca's paid
  subscription.
- A broker. `dqengine brokers` lists the adapters installed here. Alpaca
  paper is bundled. Webull and Schwab arrive with
  `pip install deployquant-webull` or `deployquant-schwab`, and both trade
  real money only.
- `pip install 'deployquant[live]'`. A plain `pip install deployquant`
  backtests and has none of the database, bus or credential packages.

## Paper trading with Alpaca

Fetch bars for what your algorithm trades, and check it backtests:

```
export APCA_API_KEY_ID=...
export APCA_API_SECRET_KEY=...
dqengine data fetch TQQQ --from 2024-01-01 --data ./data
dqengine backtest my_algo.py --data ./data
```

Start Postgres and Redis, and point the engine at them:

```
cp deploy/.env.example deploy/.env
docker compose -f deploy/docker-compose.yml up -d postgres redis

export DATABASE_URL=postgresql+psycopg2://dqengine:...@localhost:5432/dqengine
export REDIS_URL=redis://localhost:6379/0
```

Then trade it:

```
dqengine live my_algo.py --broker alpaca-paper --cash 10000
```

The first run creates the schema, one broker connection and one deployment,
and prints what it is about to do. Leave it in the foreground. Ctrl-C stops
every thread and closes the feed.

Run the same command again after editing the algorithm and it updates the
same two rows. The code snapshot and the universe are refreshed. The replay
start date, the starting cash and the leverage ceiling stay where they were
unless you pass `--start`, `--cash` or `--margin`.

## The startup block

```
dqengine live: my_algo (paper money)

  broker           alpaca-paper, mode paper
  execution truth  observe
  caps             off (pass --max-order-usd / --max-position-usd to refuse orders above a size)
  dry run          no
  universe         TQQQ
  resolution       minute
  replay from      2026-09-22
  cash / margin    $10,000.00 / 1.0x
  feed             alpaca (live bars and quotes)
  silence fallback alpaca (REST minutes while the feed is quiet)
  history          Alpaca market data (APCA_API_KEY_ID)
  bar exports      /srv/live-data
  engine           in-process (your algorithm runs in this process, with no container and no isolation from it)
  database         localhost:5432/dqengine
  deployment       6f0c...
  connection       a13e...
```

Check the first line and the caps line before you leave it running.

## Status, orders and fills

```
dqengine status          # exits 0 when everything is fine, 1 when it is not
dqengine orders          # the last 20 orders, and what happened to each
dqengine fills           # the last 20 broker executions
```

All three take `--json`. `status` exits non-zero on any of these, each read
straight from a table:

- a tick error on a running deployment, including a determinism freeze
- an error recorded by the last sweep, or by the last executions poll — a
  sweep refused by the [first-sync check](#the-first-sync-check) is one of
  these
- a broker connection the venue rejected, or one that is paused
- an order journal row that was sent and never resolved, or one abandoned
  today
- a feed that has gone silent during the session
- this connection's sweep lock still held by another process on two samples
  a second apart

A cron job or a container healthcheck can use the exit code.

`dqengine status` does not report the in-process order book. That state
lives in whichever process transmits, it is never written down, and an
empty one read from outside would say "nothing in flight" about a process
this command cannot see.

## Real money

`--live` refuses unless the broker connection's execution truth is
`enforce`. `enforce` is the mode where the broker's own fills drive the
accounting instead of the replay's model fills. `dqengine adopt` is the
command that gets a connection there.

```
dqengine adopt my_algo.py --broker alpaca --start 2026-09-01
```

It does five things, in this order:

1. creates or refreshes the same two rows `dqengine live` creates, with the
   same refusals;
2. reads the account's own trade history from the broker and works out what
   recording it would change: which entry prices move, and which day the
   ledger's boundary lands on;
3. replays the strategy from `--start` and prints what it holds next to what
   the account holds, symbol by symbol, with any disagreement marked;
4. asks for the account's label, typed back;
5. records the history, sets each deployment's `reconciled_from` boundary,
   and raises the connection to `enforce`.

Nothing is written until step 4 is answered. Type anything else and the run
stops with the ledger untouched and the connection where it was. There is no
`--yes`, and a run with no terminal refuses instead of assuming one.
`--dry-run` prints the same screen and writes nothing.

This is what the screen reads like when the replay and the account agree:

```
Adopting alpaca (dqengine live)

The strategy replays from 2026-09-01. What it holds at the end of that
replay is what the executor will hold the account to.

  SYMBOL  AT BROKER  STRATEGY
  TQQQ    40         40         agrees

Working orders at the broker: 0

Execution history the broker returned: 12 fill(s)
  2026-09-18 15:59  TQQQ   buy  40 @ 82.50
  ...
```

If they do not agree, move `--start` back and run it again. The replay
starts flat on that day and buys its way to today, so an earlier start is
what makes it hold the shares the account already has. That is the only dial
here, and the screen tells you when you have it right.

### The first-sync check

Until the two agree, the executor sends nothing at all. Before any sweep
transmits it looks at every universe symbol the account holds and asks
whether anything accounts for those shares. Two things do, and either is
enough:

- this executor has ordered the symbol on this connection before, or
- the strategy's replay holds a position in it.

If neither does, the whole sweep is refused. Nothing is sent and nothing is
cancelled, and one line names the symbol, the quantity and the way out:

```
[exec] first-sync check refused this sweep — TQQQ: the account holds 74,
this strategy holds none of it, and nothing on this connection has ever
ordered TQQQ. Nothing was sent. Reconcile the account first — `dqengine
adopt`, or the hosted platform's adoption step.
```

Without the check, a strategy pointed at an account that already holds 74
TQQQ reads `want 0, have 74` on its first sweep and market-sells shares
nobody asked it to touch. The check runs under `--dry-run` too, so a
rehearsal shows the refusal.

Four things it leaves alone:

- a symbol the account is flat in
- a quantity smaller than the venue's own minimum order quantity
- a holding outside the strategy's universe, which the executor never trades
- a quantity the two disagree on in a symbol the strategy does hold. That is
  a manual trade in a symbol the strategy trades, and the executor's
  ordinary reconciliation handles it

A deployment pointed at real money also needs `live_confirmed` on its row.
`dqengine live --live` sets it, after the `enforce` check.

Two ways to work against a real account without sending an order:

- `--dry-run` computes the real orders and sends none. Each one is recorded
  with status `dry_run` and `dqengine orders` shows it.
- paper trading, which is the default.

`dqengine adopt` leaves the connection on paper money. `dqengine live
--live` is the command that arms it. Running `adopt` again on a live
deployment puts it back to paper until the next `--live`.

## Caps

Notional caps are off by default. A cap the engine picked would bind
arbitrarily on an account whose size it does not know, and the strategy
already decided the size.

```
dqengine live my_algo.py --broker alpaca-paper \
    --max-order-usd 2500 --max-position-usd 9000
```

`--max-order-usd` refuses any single order above that notional.
`--max-position-usd` caps any one position's notional: the executor refuses
to size a position above it. A refused order is recorded with the reason, so
it shows up in `dqengine orders` rather than arriving as a position quietly
smaller than the strategy asked for.

The flags own their own settings. A later run without them clears the caps,
and the startup block then says the caps are off.

## One deployment per connection

The engine trades one deployment per broker connection. A second deployment
on the same connection is refused when the rows are created, and refused
again by the executor if one gets there another way.

Several strategies sharing one broker account need a combiner that folds
their target positions into one before anything is sent. That is not part
of this package. Give each strategy its own broker connection, or run them
on the hosted platform.

## What a deployment will not run

`dqengine live` refuses these before it writes anything:

- Second resolution. Second-resolution backtests work. Live does not
  yet.
- A daily strategy on a connection that is not `enforce`. Run
  [`dqengine adopt`](#real-money) first. See
  [Daily strategies](#daily-strategies) below.
- A non-deterministic algorithm. Every tick replays the whole history,
  and the executor reads any disagreement between two ticks as a position
  change to trade on. The screen catches `random` without a seed, network
  imports and wall-clock reads. Use `self.time`.

## Daily strategies

A strategy that subscribes at daily resolution trades live, and it fills
where its backtest fills. A scheduled check runs at its own clock time on
the previous session's close. A market order it places becomes
market-on-close, or market-on-open once the session is within 15.5 minutes
of closing, which is what LEAN does with it in a backtest.

The order reaches the broker at the moment it was decided. An at-close
order goes out a minute before the close, as a market order on a venue with
no on-close order type of its own, or as the venue's own market-on-close the
moment the ticket rests. An at-open order goes out at 09:31 the next
session. The model books both when the day's bar lands a minute after the
close, which is where a backtest of the same day books them.

The bars come from the daily files, carried past the last day the curated
files hold with rows aggregated from the minute tree. That export runs on
the deployment's own tick, so `dqengine data fetch` for the symbols is the
only preparation.

Two things are needed:

- The broker connection has to be in `enforce`. The at-close order goes out
  a minute before the close and the model settles the ticket a minute after
  it. In between only the broker's own execution rows say whether the order
  went out, and without them a missed order and a filled one read the same:
  the model's own fill then becomes an after-hours order on the next sweep.
  `dqengine adopt` is what puts a connection there — see
  [Real money](#real-money).
- The venue has to be able to place the orders the strategy uses. A market
  order is checked as itself and as the market-on-close it becomes; every
  venue in the catalog emulates market-on-close with a market order near the
  close, and the journal records the substitution.

Known limits today: two at-close orders on one symbol in one session are
refused at the payload, because they would net into one broker order and one
of them would read as a no-fill; and the minute between the close and the
bar is a minute where the order is at the venue and the model has not
settled it. For intraday timing, subscribe at minute resolution and keep the
daily indicators.

## Where your algorithm runs

`dqengine live` runs your algorithm inside its own process. A self-hosted
box runs strategies its operator wrote, so there is no container per tick
and no Docker requirement for trading. The startup block names the mode it
picked.

Set `PYRUN_ENGINE_MODE=sandbox` to run each tick in a container instead. That
needs the sandbox image and a Docker socket.

## Credentials

Broker credentials are encrypted in the database with Fernet, under a key
derived from `DQENGINE_SECRET`.

If `DQENGINE_SECRET` is unset and no key file exists, `dqengine live` writes
one to `~/.dqengine_secret` (mode 600) and prints the path, but only when the
connections table is empty. Where encrypted rows already exist it refuses
instead. A fresh key is indistinguishable from the correct one until every
stored connection fails to decrypt.

Back that file up. Losing it means reconnecting every broker.

Where the credentials come from depends on the adapter. The Alpaca paper
adapter reads `ALPACA_KEY_ID` and `ALPACA_SECRET_KEY`, or the `APCA_` pair.
Any other adapter takes them as one JSON object in
`DQENGINE_BROKER_CREDS`; the field names are in that adapter's README.

## Market data

History comes from Alpaca's market data API over your own keys. Historical
minute bars on the SIP tape are free.

The live tape is the bundled Alpaca websocket feed, selected with
`--feed alpaca`. It streams the IEX tape by default, which is one exchange.
Set `APCA_API_DATA_FEED=sip` with Alpaca's paid subscription for the full
market. `dqengine feeds` lists the feeds installed here; a broker plugin may
ship one that streams from your own account at that broker.

A stream can go quiet while the market is open. The worker notices, says so,
and fetches the minutes it missed over REST from the same feed, on the same
account and the same tape. `--fallback ID` names a different feed for those
REST bars, which is for the case where the vendor you stream from cannot
serve them. A feed that has no REST bars at all reports that in the silence
line, and the strategy then waits for the stream to come back.

The bar exports a replay reads go to `./live-data` by default, or
`PYDATA_ROOT` when you set it. That directory must not be the curated store
`dqengine backtest` reads: live bars carry raw prices and the exporter
refuses to write them over an adjusted store.

## Environment variables

`deploy/.env.example` lists all of them with a line each. Three are
required:

| Variable | What it is |
|---|---|
| `DATABASE_URL` | Postgres. Every row the executor owns lives here. |
| `REDIS_URL` | Redis. The tick, the order path and the feed meet on it. |
| `DQENGINE_SECRET` | The key broker credentials are encrypted under. |

## Running under compose

```
cp deploy/.env.example deploy/.env      # fill in the three above
mkdir -p deploy/data && cp my_algo.py deploy/data/
ALGORITHM=my_algo.py BROKER=alpaca-paper \
    docker compose -f deploy/docker-compose.yml up -d
docker compose -f deploy/docker-compose.yml logs -f dqengine
```

The dqengine service restarts unless you stop it. There is no supervisor of
ours in the image: the loop exits 0 when the deployment is stopped and
non-zero when it crashed, and Docker restarts it.

To install a broker plugin into the image, set `BROKER_PLUGIN` to its pip
name before building.

## What the hosted platform adds

DeployQuant runs this engine with more on top:

- several strategies on one broker account, folded into one set of target
  positions
- a worker fleet, with one process per deployment, supervised and restarted
- managed market data, so you bring no keys of your own
- the adoption path as a screen in the browser rather than a command
- a browser editor, backtests, charts and a strategy library
- accounts, billing and email

The order path is the same package in both, and so is the first-sync check:
the platform's own accounts run the code on this page.
