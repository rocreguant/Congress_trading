import sqlite3

import pandas as pd

from congress_trading import ranking
from congress_trading.house import parse_ptr_text

# Real text extracted from House PTR PDFs (2019 small-caps font, 2024 newer layout).
TEXT_2019 = """tranSactionS
iD owner asset transaction Date notification amount cap.
type Date gains >
$200?
SP Cigna Corporation (CI) [ST] S (partial) 03/05/2019 03/18/2019 $1,001 - $15,000 gfedc
FILINg STATUS: New
SUBHoLDINg oF: Neuberger Berman - Individual
SP Easterly government Properties, S 03/01/2019 03/18/2019 $1,001 - $15,000 gfedc
Inc. (DEA) [ST]
FILINg STATUS: New
JT Walt Disney Company (DIs) P 06/6/2016 06/6/2016 $1,001 - $15,000
"""

TEXT_2024 = """ID Owner Asset Transaction Date Notification Amount Cap.
Type Date Gains >
$200?
SP Palo Alto Networks, Inc. (PANW) P 02/12/2024 02/12/2024 $500,001 -
[OP] $1,000,000
F      S     : New
D          : Purchased 50 call options with a strike price of $200 and an expiration date of 1/17/25.
"""


def test_parse_2019_layout():
    t = parse_ptr_text(TEXT_2019)
    assert [x["ticker"] for x in t] == ["CI", "DEA", "DIS"]
    assert [x["tx_type"] for x in t] == ["sell", "sell", "buy"]
    assert t[1]["asset_type"] == "ST" and t[1]["owner"] == "SP"
    assert t[2]["tx_date"] == "2016-06-06" and t[2]["owner"] == "JT"
    assert (t[0]["amount_min"], t[0]["amount_max"]) == (1001, 15000)


def test_parse_2024_wrapped_amount_and_option():
    (t,) = parse_ptr_text(TEXT_2024)
    assert t["ticker"] == "PANW" and t["asset_type"] == "OP"
    assert (t["amount_min"], t["amount_max"]) == (500001, 1000000)
    assert t["asset"] == "Palo Alto Networks, Inc. (PANW)"
    assert "call options" in t["description"]


def _toy_db():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    from congress_trading.db import SCHEMA
    con.executescript(SCHEMA)
    con.executemany("INSERT INTO members VALUES (?,?,?,?,?,?,?,?)", [
        ("A", "Al", "Good", "Al Good", "house", "CA", 1, "D"),
        ("B", "Bo", "Bad", "Bo Bad", "house", "TX", 2, "R"),
    ])
    dates = pd.bdate_range("2020-01-01", "2026-06-30").strftime("%Y-%m-%d")
    up = [(("UP", d, 100 * 1.001 ** i)) for i, d in enumerate(dates)]
    down = [(("DOWN", d, 100 * 0.999 ** i)) for i, d in enumerate(dates)]
    spy = [(("SPY", d, 100.0)) for d in dates]
    con.executemany("INSERT INTO prices VALUES (?,?,?)", up + down + spy)
    con.execute("INSERT INTO filings(doc_id, chamber) VALUES ('x','house')")
    rows = []
    for month in range(1, 13):
        d = f"2020-{month:02d}-15"
        rows += [("x", "A", "UP", "buy", d), ("x", "A", "DOWN", "sell", d),
                 ("x", "B", "DOWN", "buy", d), ("x", "B", "UP", "sell", d)]
    rows += [("x", "B", "UP", "buy", "2026-02-02")]  # 2026 trade: must be ignored by the ranking
    con.executemany("INSERT INTO trades(doc_id, bioguide_id, ticker, tx_type, tx_date, amount_min, amount_max, "
                    "asset_type) VALUES (?,?,?,?,?,1001,15000,'ST')", rows)
    return con


def test_elo_ranks_good_trader_first_and_ignores_2026():
    con = _toy_db()
    table, scored = ranking.rank(con, cutoff="2025-12-31")
    assert list(table.bioguide_id) == ["A", "B"]
    assert table.loc[0, "weight"] > 0 > table.loc[1, "weight"]
    assert scored.tx_date.max() < "2026-01-01"
    # 5y horizon for a 2020 trade ends in 2025, so it is scored; nothing beyond the cutoff is
    assert scored["signed_5y"].notna().any()
    assert table.loc[0, "elo_composite"] > table.loc[0, "market_elo"] > table.loc[1, "elo_composite"]
