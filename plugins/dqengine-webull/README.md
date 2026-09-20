# deployquant-webull

A broker adapter that lets [DQengine](https://github.com/praneetsah/deployquant) trade a Webull US brokerage account
through Webull's OpenAPI.

```bash
pip install deployquant-webull
```

That is the whole setup on the DQengine side. The package registers itself, and
`dqengine brokers` then lists `webull`.

## The Webull SDK

The adapter uses Webull's official Python SDK (`webull-python-sdk-trade`). It
only imports the SDK when you connect, so the package installs without it.
Install the SDK when you are ready to connect an account.

The SDK's own package metadata does not install on Python 3.12. It pins a
`grpcio` version that has no build for 3.12, and it leaves out
`webull-python-sdk-mdata`, which it needs. This works:

```bash
pip install --no-deps \
    webull-python-sdk-core==0.1.18 webull-python-sdk-mdata==0.1.18 \
    webull-python-sdk-trade==0.1.18 webull-python-sdk-trade-events-core==0.1.18
pip install jmespath==1.1.0 cachetools==5.2.0 grpcio
```

The same versions are declared as the `sdk` extra
(`pip install "deployquant-webull[sdk]"`). An extra cannot pass `--no-deps`, so
on Python 3.12 use the commands above.

The adapter includes two small fixes the SDK needs on Python 3.12. They are
described at the top of `dqengine_webull/adapter.py`, along with the places
where the SDK behaves differently from its documentation.

## Credentials

You use your own Webull OpenAPI app key and app secret. Apply for them in the
OpenAPI section of your Webull account. The key pair is tied to that account.

The adapter takes them as a `creds` mapping with `app_key`, `app_secret`,
`account_id` and an optional `region`. It does not store credentials anywhere.
Where you keep them is up to you.

## No paper trading

This adapter trades real money. US accounts use Webull's v1 order endpoints
(`/trade/order/place`, `/trade/order/cancel`, `/trade/orders/list-open`). Webull
documents the v2 endpoints as available to Japan and Hong Kong customers only,
and Webull's sandbox only serves v2. So there is no working paper environment
for a US account.

## What the adapter has to get right

DQengine has a conformance test for broker adapters
(`tests/test_adapter_conformance.py`), and this plugin has to pass it. The test
checks three things. The adapter declares what it supports correctly: order
types, time in force, short selling and extended hours. It refuses an
unsupported order before anything is sent to the broker. Every broker error
comes back as one of the exception types in `dqengine.adapters.base`.

## License

[PolyForm Shield 1.0.0](https://github.com/praneetsah/deployquant/blob/main/plugins/dqengine-webull/LICENSE). You can use, change and run it for anything,
including trading your own money, except building a product or service that
competes with DQengine or with the products built on it. See `NOTICE`.
