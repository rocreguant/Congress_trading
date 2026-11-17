# Congress trading: Elo ranking of members' stock picks

Downloads every stock trade disclosed by **currently serving** members of Congress
(2016 to today), stores it in SQLite, scores each trade against the market at
1 month, 6 months, 1 year and 5 years, ranks members with Elo, and turns the
ranking into weights for buy/sell signals.

**This is research code, not investment advice.** See the caveats below.

## Data sources (all free, no API keys)

| What | Source |
|---|---|
| Current members | [`unitedstates/congress-legislators`](https://github.com/unitedstates/congress-legislators) `legislators-current.json` |
| House trades | Official Clerk of the House disclosures: yearly index `disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.zip` + one PDF per Periodic Transaction Report, parsed with `pdfplumber` |
| Senate trades | Official eFD site `efdsearch.senate.gov` (HTML tables). **Opt-in**, see below |
| Prices | Yahoo Finance public chart endpoint (daily split/dividend-adjusted closes), benchmark SPY |

Third-party aggregators (House/Senate Stock Watcher S3 buckets) are offline, and
Quiver/FMP/Capitol Trades need keys or scraping, so this goes to the official sources.

### Senate data needs your consent
`efdsearch.senate.gov` makes every user accept a usage agreement (no unlawful or
commercial use, no credit rating, no fundraising solicitation). The Senate
fetcher only runs if you pass `--accept-senate-terms`, which means *you* have read
and accept it. The bundled results below are **House only**; senators who
previously served in the House are included for their House-era trades.

## Usage

```bash
pip install -r requirements.txt
python -m congress_trading fetch                 # ~15 min: members, House PTRs, prices -> data/congress_trading.db
python -m congress_trading fetch --accept-senate-terms --skip-house --skip-prices   # add the Senate
python -m congress_trading fetch --skip-house    # refresh prices only for new tickers
python -m congress_trading rank                  # Elo ranking + weights -> output/member_rankings.csv
python -m congress_trading signals               # per-ticker BUY/SELL/HOLD -> output/signals.csv
python -m congress_trading validate              # out-of-sample check on 2026 trades
python -m congress_trading export                # all trades -> output/trades.csv.gz
python -m pytest tests
```

Re-running `fetch` is incremental: downloaded PDFs are cached in `data/cache/`
and filings already in the database are skipped.

## Database (`data/congress_trading.db`)

- `members`: bioguide id, name, chamber, state, district, party
- `filings`: one row per PTR (url, filing date, `parsed` / `scanned`)
- `trades`: owner (self/SP spouse/JT joint/DC child), asset, ticker, asset type, buy/sell, trade date, disclosure date, amount range
- `prices`: daily adjusted close per ticker
- `member_rankings`: output of `rank`

## Method

**Trade outcome.** Entry is the adjusted close on the trade date (or next trading
day). Exit is 21 / 126 / 252 / 1260 trading days later. Excess return = stock
return − SPY return over the same window. A buy scores +excess, a sale scores
−excess (a good sale is followed by the stock lagging the market). Outcomes are
clipped to ±100% so a single options-driven or bad-tick trade can't dominate.
Stocks, ETFs and stock options are scored (options by the underlying's move);
bonds, funds and other assets are skipped.

**2026 is left out.** Ranking only uses data up to `RANKING_CUTOFF = 2025-12-31`:
trades made in 2026 are excluded, *and* so is any horizon that would need a 2026
price (e.g. a June 2025 trade gets a 1m and 6m score but no 1y score). This keeps
2026 as a clean holdout for `validate`.

**Elo.** One rating per horizon. Trades are grouped by month; each month every
member who traded plays a round-robin against every other member who traded and
against a `MARKET` player whose result is always 0 excess return. The member with
the higher size-weighted average excess return wins the pairing. Start 1500, K=32
split across that month's opponents, months played chronologically.

**Composite + weight.** Composite Elo = 1m 15% + 6m 30% + 1y 35% + 5y 20%
(renormalised over the horizons a member has results for).
`weight = (2·P(beat MARKET) − 1) · n/(n+30)`, in (−1, 1): positive = follow,
negative = their trades have lagged the market (fade them), members with few
trades shrink toward 0.

**Signals.** Keyed on the **disclosure** date (members have up to 45 days to
report, so you can only act once it's public). For each ticker over the last 90
days: `score = Σ weight · (+1 buy / −1 sell) · size · 0.5^(age/30d)`, with
weights scaled so the top-ranked member = 1. `BUY` if score ≥ 0.5, `SELL` if
≤ −0.5, else `HOLD`. Tunable via `--lookback-days`, `--half-life-days`, `--as-of`.

## Results from the first run (House, 2016–2025, run 2026-11-17)

- 4,185 PTRs from current members: 3,229 electronic (parsed), 956 scanned paper (not parsed, would need OCR)
- 31,393 trades, 26,598 with a ticker, 2,665 unique tickers; prices found for 2,157 (the rest are mostly delisted/renamed)
- 150 members have enough priced trades to be ranked

2026 holdout (trades the ranking never saw), excess return vs SPY from disclosure date:

| | 1 month | 3 months |
|---|---|---|
| All trades | −0.11% | +0.24% |
| Members to follow (weight > 0) | +0.10% | +0.61% |
| Members to fade (weight < 0) | −0.35% | −0.29% |
| Top-quartile weight | +0.24% | +0.48% |
| Bottom-quartile weight | −0.56% | −1.25% |
| Spearman(weight, outcome) | −0.001 | 0.027 |

The direction is right (members ranked well kept doing better than members ranked
poorly, a ~1–1.7 point spread at 3 months), but the per-trade correlation is close
to zero, so the signal is weak and noisy.

## Caveats

- Amounts are disclosed as ranges ($1,001–$15,000 …), so size weighting is approximate.
- Many trades are made by spouses or managed accounts; they still count for the member.
- Survivorship: only current members are ranked; tickers that were delisted have no prices and are skipped.
- Parsing PDFs is heuristic; spot-check against the `filing_url` in `output/trades.csv.gz`.
- Past outperformance in this data is weak evidence of future outperformance.
