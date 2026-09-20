"""Minute-bar feeds for the reference driver.

A feed answers one question: the regular-session minute bars of `symbol`
for each session in [start, end], as [ms_since_midnight_ET, o, h, l, c, v]
rows with float prices. Backfill and the live day go through the same
call, so what the engine warmed on and what it steps today come from one
source with one shape.

AlpacaBarFeed is the bundled one. Alpaca's free tier serves full SIP
history but withholds the most recent minutes of SIP data, so the live
day defaults to the IEX feed (`feed="iex"`); pass `feed="sip"` with a
paid data subscription. The engine trades the regular session only, so
pre/post-market bars are dropped here, and the 13:00 close on half-days
comes from the same calendar the engine uses.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime
from typing import Protocol
from zoneinfo import ZoneInfo

from dqengine.runtime.core.data import REG_OPEN_MS, close_time_ms

ET = ZoneInfo("America/New_York")
BARS_URL = "https://data.alpaca.markets/v2/stocks/{sym}/bars"
PAGE_LIMIT = 10000


class BarFeed(Protocol):
    def fetch_days(self, symbol: str, start: date, end: date) -> dict[date, list[list]]: ...


def bars_to_days(bars: list[dict]) -> dict[date, list[list]]:
    """Alpaca bar dicts (RFC3339 UTC bar-start times) -> {ET date: rows},
    regular session only, rows sorted by minute."""
    out: dict[date, list[list]] = {}
    for b in bars:
        dt = datetime.fromisoformat(b["t"].replace("Z", "+00:00")).astimezone(ET)
        ms = (dt.hour * 3600 + dt.minute * 60 + dt.second) * 1000
        if ms < REG_OPEN_MS or ms >= close_time_ms(dt.date()):
            continue
        out.setdefault(dt.date(), []).append(
            [ms, float(b["o"]), float(b["h"]), float(b["l"]), float(b["c"]), float(b["v"])])
    for rows in out.values():
        rows.sort()
    return out


def drop_in_progress(rows: list, now_ms: int, bar_ms: int = 60_000) -> list:
    """Only bars that have CLOSED by `now_ms`. The in-progress minute is the
    one bar a feed can hand over that a replay would later read differently;
    it must never reach the engine."""
    return [r for r in rows if int(r[0]) + bar_ms <= now_ms]


class AlpacaBarFeed:
    def __init__(self, key_id: str, secret_key: str, feed: str = "iex",
                 adjustment: str = "all", timeout: float = 60.0, sleep=time.sleep):
        self.key_id, self.secret_key = key_id, secret_key
        self.feed, self.adjustment, self.timeout = feed, adjustment, timeout
        self._sleep = sleep

    def _get(self, url: str, params: dict) -> dict:
        req = urllib.request.Request(
            f"{url}?{urllib.parse.urlencode(params)}",
            headers={"APCA-API-KEY-ID": self.key_id, "APCA-API-SECRET-KEY": self.secret_key})
        for attempt in range(5):
            try:
                return json.load(urllib.request.urlopen(req, timeout=self.timeout))
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < 4:          # free tier: 200 req/min
                    self._sleep(15 * (attempt + 1))
                    continue
                body = e.read().decode(errors="replace")[:200]
                raise RuntimeError(f"Alpaca data API {e.code}: {body}") from e
        raise RuntimeError("Alpaca data API: retries exhausted")

    def fetch_days(self, symbol: str, start: date, end: date) -> dict[date, list[list]]:
        params = {"timeframe": "1Min", "adjustment": self.adjustment, "feed": self.feed,
                  "limit": PAGE_LIMIT,
                  "start": f"{start.isoformat()}T00:00:00Z",
                  "end": f"{end.isoformat()}T23:59:59Z"}
        days: dict[date, list[list]] = {}
        while True:
            d = self._get(BARS_URL.format(sym=symbol.upper()), params)
            for day, rows in bars_to_days(d.get("bars") or []).items():
                days.setdefault(day, []).extend(rows)
            token = d.get("next_page_token")
            if not token:
                break
            params["page_token"] = token
        for rows in days.values():
            rows.sort()
        return days
