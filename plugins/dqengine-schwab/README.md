# deployquant-schwab

A broker adapter that lets [DQengine](https://github.com/praneetsah/deployquant) trade a Charles Schwab brokerage
account through the Schwab Trader API, and a market data feed that streams
quotes and minute bars from the same account.

```bash
pip install deployquant-schwab
```

That is the whole setup on the DQengine side. The package registers itself,
`dqengine brokers` then lists `schwab`, and so does `dqengine feeds`.

The adapter talks to the Trader API directly with Python's standard library. It
needs no Schwab SDK. The market data feed needs a websocket client, which is the
one optional extra:

```bash
pip install 'deployquant-schwab[live]'
```

## Credentials

You use your own Schwab developer app. Register an app in the Schwab developer
portal. The redirect URI has to be HTTPS and has to be registered there. After
you authorize the app, you have an app key, an app secret and a refresh token.

The adapter takes them as a `creds` mapping with `app_key`, `app_secret`,
`refresh_token` and an optional `account_number`. When it refreshes the access
token or looks up the account, it gives you back an updated mapping. It does not
store credentials anywhere.

`dqengine_schwab.oauth` does the token refresh, which is one POST to Schwab's
token endpoint. Your application runs the first two steps of the OAuth flow:
building the authorization URL and exchanging the code.

Schwab's refresh tokens expire after seven days. When that happens the adapter
raises `BrokerAuthExpired`, and the account owner has to authorize again.

## Market data feed

`SchwabQuoteFeed` streams two things from the Schwab streamer socket: one
minute bar per symbol as each minute closes, and level one quotes with bid, ask,
last price, day volume, high, low, previous close, open and security status. The
bars are regular session only. Schwab keeps sending candles until 16:00 on an
early close day, and those are dropped rather than stored as regular bars.

Select it by name, the same way a broker adapter is selected:

```python
from dqengine.feeds import MinuteZipStore, get_feed

feed = get_feed("schwab", creds=creds, store=MinuteZipStore("./data"),
                symbols=["TQQQ"], on_bar=print)
while True:
    feed.poll(timeout=1.0)
```

`creds` is the same mapping the broker adapter takes. The feed uses your own
Schwab developer app and your own Schwab account, and it mints an access token
from the refresh token in that mapping. If you keep tokens somewhere else, pass
`token=` a callable that returns an access token instead.

Schwab allows one streamer connection per user. If you run the feed in two
processes against the same Schwab login, they compete for that connection.

Schwab's refresh tokens expire seven days after they are issued. When that
happens the feed stops connecting, writes what to do into `feed.state.error`,
logs the same line, and waits five minutes between attempts. Renewing the token
means authorizing the app again and storing the new refresh token.

The feed does not fetch history. It streams what happens from the moment it
connects.

It does fetch recent minutes. `feed.refresh_bars("TQQQ")` asks Schwab's
price history for the last few sessions of that symbol and writes the
regular-session minutes into the `bar_days` table, using the same token
callable the stream uses. That is what a live worker calls while the stream
is quiet: the bars it missed come from the account it was streaming, not
from another vendor. A day already stored with at least as many minutes as
came back is left alone, and a thinner one is replaced. Writing bars needs
the database, so this call needs `deployquant[live]` installed; streaming
and trading do not.

## No paper trading

Schwab has no paper environment, so this adapter trades real money. The adapter
reports short selling as unsupported until it has been checked on a real
account.

## What the adapter has to get right

DQengine has a conformance test for broker adapters
(`tests/test_adapter_conformance.py`), and this plugin has to pass it. The test
checks three things. The adapter declares what it supports correctly: order
types, time in force, short selling and extended hours. It refuses an
unsupported order before anything is sent to the broker. Every broker error
comes back as one of the exception types in `dqengine.adapters.base`.

Schwab rejects bad orders after accepting them. The order is created first and
then shows up as `REJECTED`. The adapter keeps such an order visible until it
reaches a final state.

## License

[PolyForm Shield 1.0.0](https://github.com/praneetsah/deployquant/blob/main/plugins/dqengine-schwab/LICENSE). You can use, change and run it for anything,
including trading your own money, except building a product or service that
competes with DQengine or with the products built on it. See `NOTICE`.
