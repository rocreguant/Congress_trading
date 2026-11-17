"""Command line entry point.

    python -m congress_trading fetch            # members + House PTRs + prices -> SQLite
    python -m congress_trading fetch --accept-senate-terms   # also Senate (see senate.py)
    python -m congress_trading rank             # Elo ranking + weights (2016-2025 only)
    python -m congress_trading signals          # buy/sell signals from recent disclosures
    python -m congress_trading validate         # out-of-sample check on 2026 trades
    python -m congress_trading export           # trades -> output/trades.csv.gz
"""
from __future__ import annotations

import argparse
import json
import logging

import pandas as pd

from . import config, db, house, members, prices, ranking, senate, signals


def _load_weights(con) -> pd.DataFrame:
    try:
        return pd.read_sql_query("SELECT * FROM member_rankings", con)
    except Exception:
        table, _ = ranking.rank(con)
        ranking.save_weights(con, table)
        return table


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="congress_trading")
    ap.add_argument("--db", default=str(config.DB_PATH))
    sub = ap.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("fetch", help="download members, disclosures and prices into SQLite")
    f.add_argument("--first-year", type=int, default=config.FIRST_YEAR)
    f.add_argument("--last-year", type=int, default=config.LAST_YEAR)
    f.add_argument("--skip-house", action="store_true")
    f.add_argument("--skip-prices", action="store_true")
    f.add_argument("--accept-senate-terms", action="store_true",
                   help="I have read and accept the efdsearch.senate.gov usage agreement; fetch Senate PTRs")

    r = sub.add_parser("rank", help="rank members with Elo and save weights")
    r.add_argument("--cutoff", default=config.RANKING_CUTOFF)
    r.add_argument("--top", type=int, default=25)

    s = sub.add_parser("signals", help="per-ticker buy/sell signals from recent disclosures")
    s.add_argument("--as-of", default=None, help="YYYY-MM-DD (default today)")
    s.add_argument("--lookback-days", type=int, default=90)
    s.add_argument("--half-life-days", type=float, default=30)
    s.add_argument("--top", type=int, default=30)

    v = sub.add_parser("validate", help="out-of-sample test of the weights on 2026 trades")
    v.add_argument("--start", default="2026-01-01")
    v.add_argument("--horizon-days", type=int, default=21)

    sub.add_parser("export", help="write all trades (joined with member names) to output/trades.csv.gz")

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    con = db.connect(args.db)
    config.OUTPUT_DIR.mkdir(exist_ok=True)
    pd.set_option("display.width", 200)
    pd.set_option("display.max_columns", 20)

    if args.cmd == "fetch":
        years = range(args.first_year, args.last_year + 1)
        logging.info("Current members: %d", members.fetch_current_members(con))
        if not args.skip_house:
            house.sync(con, years)
        if args.accept_senate_terms:
            senate.sync(con, years)
        else:
            logging.info("Skipping Senate: pass --accept-senate-terms after reading the eFD usage agreement")
        if not args.skip_prices:
            prices.sync(con)

    elif args.cmd == "rank":
        table, scored = ranking.rank(con, cutoff=args.cutoff)
        ranking.save_weights(con, table)
        table.to_csv(config.OUTPUT_DIR / "member_rankings.csv", index=False)
        cols = ["rank", "name", "party", "state", "n_trades", "elo_1m", "elo_6m", "elo_1y", "elo_5y",
                "elo_composite", "avg_excess_1y", "hit_rate_1y", "weight"]
        print(f"MARKET composite Elo: {table.market_elo.iloc[0]}  (members above it beat SPY more often than not)")
        print(table[cols].head(args.top).to_string(index=False))
        print("\nBottom:")
        print(table[cols].dropna(subset=["elo_composite"]).tail(10).to_string(index=False))
        print(f"\nSaved {len(table)} members to {config.OUTPUT_DIR / 'member_rankings.csv'} and table member_rankings")

    elif args.cmd == "signals":
        out = signals.generate(con, _load_weights(con), as_of=args.as_of, lookback_days=args.lookback_days,
                               half_life_days=args.half_life_days)
        out.to_csv(config.OUTPUT_DIR / "signals.csv", index=False)
        print(out.head(args.top).to_string(index=False))

    elif args.cmd == "export":
        df = pd.read_sql_query(
            "SELECT m.full_name, m.party, m.state, m.chamber AS current_chamber, t.*, f.url AS filing_url "
            "FROM trades t JOIN members m USING (bioguide_id) JOIN filings f USING (doc_id) "
            "ORDER BY t.tx_date", con)
        path = config.OUTPUT_DIR / "trades.csv.gz"
        df.to_csv(path, index=False)
        print(f"Wrote {len(df)} trades to {path}")

    elif args.cmd == "validate":
        res = signals.validate_holdout(con, _load_weights(con), start=args.start, horizon_days=args.horizon_days)
        print(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()
