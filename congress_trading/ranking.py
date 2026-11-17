"""Score every trade and rank members with Elo.

1. Trade outcome
   For each buy/sell with a ticker, take the adjusted close on the trade date (or the
   next trading day) and again 1 month, 6 months, 1 year and 5 years later
   (21/126/252/1260 trading days). The *excess return* is the stock's return minus
   SPY's over the same window. A purchase scores +excess, a sale scores -excess (a good
   sale is one followed by the stock lagging the market).

   No price after RANKING_CUTOFF (2025-12-31) is used: trades made in 2026 are left
   out, and so is any horizon whose end date would fall in 2026.

2. Elo, one rating per horizon
   Trades are grouped by the calendar month they were made in. Each month every member
   who traded plays a round-robin against every other member who traded that month, plus
   a fixed "MARKET" player whose result is always 0 excess return. Whoever had the higher
   (size-weighted) average signed excess return wins the pairing. Ratings start at 1500,
   K=32 split across the opponents of the month, so one month moves a rating by at most
   about K points. Months are played in chronological order.

3. Composite rating and weight
   composite = weighted mean of the horizon Elos (1m 15%, 6m 30%, 1y 35%, 5y 20%),
   renormalised over the horizons the member actually has results for.
   weight = (2 * P(beat MARKET) - 1) * n / (n + 30), where P is the Elo expected score
   against the MARKET player and n the number of scored trades. It lies in (-1, 1):
   positive = follow this member, negative = their trades have lagged the market
   (fade them), and members with few trades are pulled toward 0.
"""
from __future__ import annotations

import logging
import math
from collections import defaultdict

import numpy as np
import pandas as pd

from . import config

log = logging.getLogger(__name__)

MARKET = "MARKET"
START_ELO = 1500.0
K = 32.0
CONFIDENCE_N = 30
TRADEABLE_TYPES = ("ST", "OP", "EF")  # stocks, options on stocks, ETFs; NULL type with a ticker also allowed
ENTRY_SLACK_DAYS = 5  # trading days allowed between trade date and first available price


def size_weight(amount_min, amount_max) -> float:
    """Bigger disclosed amounts count a bit more: 1 for <= $15k, ~2 for $250k, ~3 for $1M+."""
    lo = amount_min or 1000.0
    hi = amount_max or lo
    mid = max((lo + hi) / 2, 1000.0)
    return max(1.0, 1.0 + math.log10(mid / 15000.0) * 1.2)


def load_trades(con, start: str | None = None, end: str | None = None) -> pd.DataFrame:
    q = """
        SELECT t.id, t.bioguide_id, m.full_name, m.party, m.state, m.chamber AS current_chamber,
               t.ticker, t.asset_type, t.tx_type, t.tx_date, t.notification_date,
               t.amount_min, t.amount_max, t.owner
        FROM trades t JOIN members m USING (bioguide_id)
        WHERE t.ticker IS NOT NULL AND t.ticker != '' AND t.tx_type IN ('buy', 'sell')
          AND t.tx_date IS NOT NULL
          AND (t.asset_type IS NULL OR t.asset_type IN ({}))
    """.format(",".join("?" * len(TRADEABLE_TYPES)))
    params = list(TRADEABLE_TYPES)
    if start:
        q += " AND t.tx_date >= ?"
        params.append(start)
    if end:
        q += " AND t.tx_date <= ?"
        params.append(end)
    df = pd.read_sql_query(q, con, params=params)
    # typo'd dates in filings ('2202-03-01'): keep trades dated plausibly and not after disclosure
    disclosed = df["notification_date"].fillna(df["tx_date"])
    df = df[(df.tx_date >= f"{config.FIRST_YEAR - 2}-01-01") & (df.tx_date <= disclosed)]
    # amended PTRs repeat the original trades under a new DocID
    df = df.drop_duplicates(subset=["bioguide_id", "ticker", "tx_type", "tx_date", "amount_min", "owner"])
    df = df.reset_index(drop=True)
    df["size_w"] = [size_weight(a, b) for a, b in zip(df.amount_min, df.amount_max)]
    return df


class PriceBook:
    """Adjusted closes per ticker, truncated at `cutoff` so nothing later can leak in."""

    def __init__(self, con, cutoff: str | None):
        q = "SELECT ticker, date, adjclose FROM prices"
        params = []
        if cutoff:
            q += " WHERE date <= ?"
            params.append(cutoff)
        q += " ORDER BY ticker, date"
        df = pd.read_sql_query(q, con, params=params)
        self.series: dict[str, tuple[np.ndarray, np.ndarray]] = {}
        for t, g in df.groupby("ticker", sort=False):
            self.series[t] = (g["date"].to_numpy(dtype="U10"), g["adjclose"].to_numpy(dtype=float))

    def window(self, ticker: str, date: str, horizon: int):
        """(entry_date, entry_price, exit_date, exit_price) or None if not observable."""
        s = self.series.get(ticker)
        if s is None:
            return None
        dates, px = s
        i = int(np.searchsorted(dates, date))
        if i >= len(dates) or _tdays_gap(dates[i], date) > ENTRY_SLACK_DAYS:
            return None
        j = i + horizon
        if j >= len(dates) or px[i] <= 0:
            return None
        return dates[i], px[i], dates[j], px[j]

    def ret_between(self, ticker: str, d0: str, d1: str):
        s = self.series.get(ticker)
        if s is None:
            return None
        dates, px = s
        i, j = int(np.searchsorted(dates, d0)), int(np.searchsorted(dates, d1))
        if j >= len(dates) or i >= len(dates) or px[i] <= 0:
            return None
        return px[j] / px[i] - 1.0


def _tdays_gap(d_found: str, d_wanted: str) -> int:
    return np.busday_count(d_wanted, d_found)


def score_trades(trades: pd.DataFrame, book: PriceBook, horizons=None, entry_col: str = "tx_date") -> pd.DataFrame:
    """Add ret_<h>, xs_<h> (excess vs SPY) and signed_<h> columns for each horizon."""
    horizons = horizons or config.HORIZONS
    out = trades.copy()
    direction = np.where(out["tx_type"] == "buy", 1.0, -1.0)
    for name, h in horizons.items():
        rets, xs = [], []
        for ticker, d in zip(out["ticker"], out[entry_col]):
            w = book.window(ticker, d, h) if isinstance(d, str) else None
            if w is None:
                rets.append(np.nan)
                xs.append(np.nan)
                continue
            d0, p0, d1, p1 = w
            r = p1 / p0 - 1.0
            b = book.ret_between(config.BENCHMARK, d0, d1)
            rets.append(r)
            xs.append(np.nan if b is None else r - b)
        out[f"ret_{name}"] = rets
        out[f"xs_{name}"] = xs
        # clip extreme outcomes (options-driven or bad ticks) so one trade can't dominate
        out[f"signed_{name}"] = np.clip(direction * np.array(xs), -1.0, 1.0)
    return out


def elo_for_horizon(scored: pd.DataFrame, horizon: str) -> tuple[dict[str, float], dict[str, int]]:
    col = f"signed_{horizon}"
    df = scored.dropna(subset=[col]).copy()
    df["month"] = df["tx_date"].str[:7]
    ratings: dict[str, float] = defaultdict(lambda: START_ELO)
    games: dict[str, int] = defaultdict(int)
    ratings[MARKET] = START_ELO
    for _month, g in df.groupby("month", sort=True):
        perf = (g[col] * g["size_w"]).groupby(g["bioguide_id"]).sum() / g["size_w"].groupby(g["bioguide_id"]).sum()
        players = list(perf.index) + [MARKET]
        score = dict(perf.items())
        score[MARKET] = 0.0
        if len(players) < 2:
            continue
        k = K / (len(players) - 1)
        delta = defaultdict(float)
        for a_i in range(len(players)):
            a = players[a_i]
            for b in players[a_i + 1:]:
                ea = 1.0 / (1.0 + 10 ** ((ratings[b] - ratings[a]) / 400.0))
                sa = 1.0 if score[a] > score[b] else 0.0 if score[a] < score[b] else 0.5
                delta[a] += k * (sa - ea)
                delta[b] -= k * (sa - ea)
        for p, d in delta.items():
            ratings[p] += d
            games[p] += 1
    return dict(ratings), dict(games)


def rank(con, cutoff: str = config.RANKING_CUTOFF) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (member ranking table, scored trades)."""
    trades = load_trades(con, end=cutoff)
    book = PriceBook(con, cutoff)
    scored = score_trades(trades, book)

    elos, months = {}, {}
    for h in config.HORIZONS:
        elos[h], months[h] = elo_for_horizon(scored, h)

    members = pd.read_sql_query("SELECT bioguide_id, full_name, party, state, chamber FROM members", con)
    members = members[members.bioguide_id.isin(trades.bioguide_id.unique())].set_index("bioguide_id")
    rows = []
    for bio, m in members.iterrows():
        mt = scored[scored.bioguide_id == bio]
        row = dict(bioguide_id=bio, name=m.full_name, party=m.party, state=m.state, chamber=m.chamber,
                   n_trades=len(mt), n_buys=int((mt.tx_type == "buy").sum()), n_sells=int((mt.tx_type == "sell").sum()))
        num = den = 0.0
        for h, w in config.HORIZON_WEIGHTS.items():
            s = mt[f"signed_{h}"].dropna()
            row[f"elo_{h}"] = round(elos[h].get(bio, np.nan), 1) if bio in elos[h] else np.nan
            row[f"n_{h}"] = len(s)
            row[f"avg_excess_{h}"] = round(s.mean(), 4) if len(s) else np.nan
            row[f"hit_rate_{h}"] = round((s > 0).mean(), 3) if len(s) else np.nan
            if bio in elos[h]:
                num += w * elos[h][bio]
                den += w
        row["elo_composite"] = round(num / den, 1) if den else np.nan
        rows.append(row)
    table = pd.DataFrame(rows)

    m_num = sum(w * elos[h][MARKET] for h, w in config.HORIZON_WEIGHTS.items())
    market_elo = m_num / sum(config.HORIZON_WEIGHTS.values())
    n_scored = table[[f"n_{h}" for h in config.HORIZONS]].max(axis=1)
    p_beat = 1.0 / (1.0 + 10 ** ((market_elo - table["elo_composite"]) / 400.0))
    table["weight"] = ((2 * p_beat - 1) * n_scored / (n_scored + CONFIDENCE_N)).round(4)
    table.loc[table["elo_composite"].isna(), "weight"] = 0.0
    table["market_elo"] = round(market_elo, 1)
    table = table.sort_values("elo_composite", ascending=False, na_position="last").reset_index(drop=True)
    table.insert(0, "rank", range(1, len(table) + 1))
    return table, scored


def save_weights(con, table: pd.DataFrame) -> None:
    con.execute("DROP TABLE IF EXISTS member_rankings")
    table.to_sql("member_rankings", con, index=False)
    con.commit()
