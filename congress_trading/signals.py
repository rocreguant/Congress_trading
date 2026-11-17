"""Turn member weights + recent disclosures into per-ticker buy/sell signals, and test them on 2026.

Signals are keyed on the *disclosure* date, not the trade date: members have up to
45 days to report, so you can only act once a trade is public.

score(ticker) = sum over recent disclosed trades of
                weight(member) * (+1 buy / -1 sell) * size_weight * 0.5 ** (age_days / half_life)

A positive score means skilled members have been buying (or unskilled members selling);
a negative score means the opposite.
"""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd

from . import config
from .ranking import PriceBook, load_trades, score_trades

BUY_THRESHOLD = 0.5
SELL_THRESHOLD = -0.5


def generate(con, weights: pd.DataFrame, as_of: str | None = None, lookback_days: int = 90,
             half_life_days: float = 30.0, min_abs_weight: float = 0.0) -> pd.DataFrame:
    as_of = as_of or date.today().isoformat()
    start = (date.fromisoformat(as_of) - timedelta(days=lookback_days)).isoformat()
    t = load_trades(con)
    t["disclosed"] = t["notification_date"].fillna(t["tx_date"])
    t = t[(t.disclosed >= start) & (t.disclosed <= as_of)]
    w = weights.set_index("bioguide_id")["weight"]
    w = w / max(w.abs().max(), 1e-9)  # best-ranked member = 1.0, so thresholds are scale-free
    t = t.assign(weight=t.bioguide_id.map(w).fillna(0.0))
    t = t[t.weight.abs() > min_abs_weight]
    if t.empty:
        return pd.DataFrame(columns=["ticker", "score", "signal"])
    age = (pd.Timestamp(as_of) - pd.to_datetime(t.disclosed)).dt.days.clip(lower=0)
    direction = np.where(t.tx_type == "buy", 1.0, -1.0)
    t = t.assign(contrib=t.weight * direction * t.size_w * 0.5 ** (age / half_life_days))

    def names(g, side):
        return ", ".join(sorted(set(g.loc[g.tx_type == side, "full_name"])))

    rows = []
    for ticker, g in t.groupby("ticker"):
        rows.append(dict(
            ticker=ticker, score=round(g.contrib.sum(), 3), n_trades=len(g),
            n_members=g.bioguide_id.nunique(), buyers=names(g, "buy"), sellers=names(g, "sell"),
            last_disclosed=g.disclosed.max(),
        ))
    out = pd.DataFrame(rows)
    out["signal"] = np.select([out.score >= BUY_THRESHOLD, out.score <= SELL_THRESHOLD], ["BUY", "SELL"], "HOLD")
    return out.sort_values("score", ascending=False, key=abs).reset_index(drop=True)


def validate_holdout(con, weights: pd.DataFrame, start: str = "2026-01-01", horizon_days: int = 21) -> dict:
    """Out-of-sample check on trades never used for ranking (default: 2026).

    For each 2026 trade, measure the signed excess return vs SPY from the *disclosure*
    date over `horizon_days` trading days, and compare members the 2016-2025 ranking
    said to follow (weight > 0) against those it said to fade (weight < 0).
    """
    t = load_trades(con, start=start)
    if t.empty:
        return {"n_trades": 0}
    t["disclosed"] = t["notification_date"].fillna(t["tx_date"])
    book = PriceBook(con, cutoff=None)
    s = score_trades(t, book, horizons={"h": horizon_days}, entry_col="disclosed").dropna(subset=["signed_h"])
    s = s.assign(weight=s.bioguide_id.map(weights.set_index("bioguide_id")["weight"]).fillna(0.0))
    pos, neg = s[s.weight > 0], s[s.weight < 0]
    q = s.weight.quantile([0.25, 0.75]) if len(s) else None
    top, bottom = (s[s.weight >= q[0.75]], s[s.weight <= q[0.25]]) if q is not None else (s, s)
    corr = s[["weight", "signed_h"]].corr(method="spearman").iloc[0, 1] if len(s) > 2 else np.nan
    return {
        "horizon_trading_days": horizon_days,
        "n_trades_scored": len(s),
        "mean_signed_excess_all": round(s.signed_h.mean(), 4),
        "mean_signed_excess_follow (weight>0)": round(pos.signed_h.mean(), 4) if len(pos) else None,
        "mean_signed_excess_fade (weight<0)": round(neg.signed_h.mean(), 4) if len(neg) else None,
        "mean_signed_excess_top_quartile_weight": round(top.signed_h.mean(), 4),
        "mean_signed_excess_bottom_quartile_weight": round(bottom.signed_h.mean(), 4),
        "weighted_signal_return": round((np.sign(s.weight) * s.signed_h).mean(), 4),
        "spearman_weight_vs_outcome": round(float(corr), 4),
    }
