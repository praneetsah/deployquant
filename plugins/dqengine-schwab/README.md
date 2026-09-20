# deployquant-schwab

A broker adapter that lets [DQengine](https://github.com/praneetsah/deployquant) trade a Charles Schwab brokerage
account through the Schwab Trader API.

```bash
pip install deployquant-schwab
```

That is the whole setup on the DQengine side. The package registers itself, and
`dqengine brokers` then lists `schwab`.

The adapter talks to the Trader API directly with Python's standard library. It
needs no Schwab SDK and has no optional extras.

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
