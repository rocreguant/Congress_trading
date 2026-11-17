"""Daily split/dividend-adjusted closes from Yahoo Finance's public chart endpoint (no key needed)."""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import requests

from . import config

log = logging.getLogger(__name__)

_session = requests.Session()
_session.headers["User-Agent"] = config.USER_AGENT

START = int(datetime(config.FIRST_YEAR - 1, 1, 1, tzinfo=timezone.utc).timestamp())


def yahoo_symbol(ticker: str) -> str:
    return ticker.replace(".", "-").replace("/", "-")  # BRK.B -> BRK-B


def fetch_history(ticker: str) -> list[tuple[str, float]]:
    url = config.YAHOO_CHART_URL.format(ticker=yahoo_symbol(ticker))
    params = {"period1": START, "period2": int(time.time()), "interval": "1d", "events": "div|split"}
    for attempt in range(4):
        r = _session.get(url, params=params, timeout=60)
        if r.status_code == 404:
            return []
        if r.status_code == 429:
            time.sleep(5 * (attempt + 1))
            continue
        r.raise_for_status()
        break
    else:
        raise RuntimeError(f"rate limited fetching {ticker}")
    res = (r.json().get("chart") or {}).get("result")
    if not res or "timestamp" not in res[0]:
        return []
    res = res[0]
    adj = res["indicators"].get("adjclose", [{}])[0].get("adjclose") or res["indicators"]["quote"][0]["close"]
    out = []
    for ts, px in zip(res["timestamp"], adj):
        if px is not None:
            out.append((datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat(), float(px)))
    return out


def sync(con, workers: int = 4, refresh: bool = False) -> None:
    """Fetch prices for every traded ticker plus the benchmark."""
    tickers = {r[0] for r in con.execute(
        "SELECT DISTINCT ticker FROM trades WHERE ticker IS NOT NULL AND ticker != ''")}
    tickers.add(config.BENCHMARK)
    if not refresh:
        tickers -= {r[0] for r in con.execute("SELECT ticker FROM price_fetch")}
    log.info("Prices: fetching %d tickers", len(tickers))
    now = datetime.now(timezone.utc).isoformat()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(fetch_history, t): t for t in sorted(tickers)}
        for n, fut in enumerate(as_completed(futs), 1):
            t = futs[fut]
            try:
                rows = fut.result()
            except Exception as exc:
                log.warning("Price fetch failed for %s: %s", t, exc)
                continue
            con.executemany("INSERT OR REPLACE INTO prices VALUES (?,?,?)", [(t, d, p) for d, p in rows])
            con.execute("INSERT OR REPLACE INTO price_fetch VALUES (?,?,?)", (t, "ok" if rows else "missing", now))
            if n % 100 == 0:
                con.commit()
                log.info("Prices: %d/%d", n, len(tickers))
    con.commit()
