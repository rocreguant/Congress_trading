"""Shared configuration."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
CACHE_DIR = DATA_DIR / "cache"
DB_PATH = DATA_DIR / "congress_trading.db"
OUTPUT_DIR = ROOT / "output"

USER_AGENT = "Mozilla/5.0 (congress-trading research script)"

# Years of disclosures to download (inclusive). 2026 is downloaded so it can be
# used as an out-of-sample holdout, but it is never used to rank members.
FIRST_YEAR = 2016
LAST_YEAR = 2026

# Everything used to *rank* members must be on or before this date: trades made
# after it are excluded, and so are price observations after it (a 1-year
# return on a 2025-06 trade would need 2026 prices, so it is left unscored).
RANKING_CUTOFF = "2025-12-31"

# Horizons in trading days (~21 per month).
HORIZONS = {"1m": 21, "6m": 126, "1y": 252, "5y": 1260}
# How much each horizon contributes to the composite Elo.
HORIZON_WEIGHTS = {"1m": 0.15, "6m": 0.30, "1y": 0.35, "5y": 0.20}

BENCHMARK = "SPY"

LEGISLATORS_URL = (
    "https://raw.githubusercontent.com/unitedstates/congress-legislators/"
    "gh-pages/legislators-current.json"
)
HOUSE_INDEX_URL = "https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.zip"
HOUSE_PTR_URL = "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{year}/{doc_id}.pdf"
SENATE_BASE = "https://efdsearch.senate.gov"
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
