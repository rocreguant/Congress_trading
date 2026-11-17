"""Senate Periodic Transaction Reports from the official eFD site (efdsearch.senate.gov).

The site is free and needs no API key, but before searching it asks every user to
accept a usage agreement, which says the data may not be used for:
  * any unlawful purpose,
  * any commercial purpose, other than by news and communications media for
    dissemination to the general public,
  * determining or establishing the credit rating of any individual,
  * soliciting money for any political, charitable or other purpose.
(5 U.S.C. app. 13107(c)). Read it at https://efdsearch.senate.gov/search/home/.

Because of that, this module only runs when you pass `--accept-senate-terms` on
the command line, which records that *you* have read and accept the agreement.

Electronically filed PTRs are HTML tables and are parsed here. Paper filings are
scanned images and are recorded with status 'scanned' but not parsed.
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime
from html import unescape

import requests

from . import config
from .members import MemberMatcher

log = logging.getLogger(__name__)

PTR_REPORT_TYPE = 11
ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
CELL_RE = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S)
TAG_RE = re.compile(r"<[^>]+>")
TX_MAP = {"Purchase": "buy", "Sale (Full)": "sell", "Sale (Partial)": "sell", "Sale": "sell", "Exchange": "exchange"}
OWNER_MAP = {"Self": "self", "Spouse": "SP", "Joint": "JT", "Child": "DC"}
ASSET_TYPE_MAP = {"Stock": "ST", "Stock Option": "OP", "Options": "OP", "Other Securities": "OT"}


def _clean(html: str) -> str:
    return re.sub(r"\s+", " ", unescape(TAG_RE.sub(" ", html))).strip()


def _iso(d: str, fmt: str = "%m/%d/%Y") -> str | None:
    try:
        return datetime.strptime(d.strip(), fmt).date().isoformat()
    except ValueError:
        return None


def _amount(text: str) -> tuple[float | None, float | None]:
    vals = [float(v.replace(",", "")) for v in re.findall(r"\$([\d,]+(?:\.\d+)?)", text)]
    if not vals:
        return None, None
    if len(vals) >= 2:
        return vals[0], vals[1]
    return (vals[0], vals[0] * 2) if "over" in text.lower() else (vals[0], vals[0])


class SenateClient:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers["User-Agent"] = config.USER_AGENT
        self.base = config.SENATE_BASE

    def accept_terms(self) -> None:
        home = self.base + "/search/home/"
        r = self.s.get(home, timeout=60)
        r.raise_for_status()
        token = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', r.text).group(1)
        r = self.s.post(home, data={"prohibition_agreement": "1", "csrfmiddlewaretoken": token},
                        headers={"Referer": home}, timeout=60)
        r.raise_for_status()

    def search_ptrs(self, start_date: str, end_date: str):
        """Yield (first, last, report_url, submitted_date) for senator PTRs filed in the range (MM/DD/YYYY)."""
        csrf = self.s.cookies.get("csrftoken")
        start = 0
        while True:
            data = {
                "start": str(start), "length": "100", "report_types": f"[{PTR_REPORT_TYPE}]",
                "filer_types": "[1]",  # 1 = Senator
                "submitted_start_date": f"{start_date} 00:00:00", "submitted_end_date": f"{end_date} 23:59:59",
                "candidate_state": "", "senator_state": "", "office_id": "", "first_name": "", "last_name": "",
                "csrfmiddlewaretoken": csrf,
            }
            r = self.s.post(self.base + "/search/report/data/", data=data,
                            headers={"Referer": self.base + "/search/", "X-CSRFToken": csrf}, timeout=60)
            r.raise_for_status()
            j = r.json()
            for first, last, _office, link_html, submitted in j["data"]:
                href = re.search(r'href="([^"]+)"', link_html).group(1)
                yield first.strip(), last.strip(), self.base + href, submitted
            start += 100
            if start >= j["recordsFiltered"]:
                break
            time.sleep(0.5)

    def parse_ptr(self, url: str) -> list[dict]:
        r = self.s.get(url, timeout=60)
        r.raise_for_status()
        html = r.text
        table = html[html.find("<tbody"): html.find("</tbody>")]
        trades = []
        for row in ROW_RE.findall(table):
            cells = [_clean(c) for c in CELL_RE.findall(row)]
            if len(cells) < 9:
                continue
            # columns: #, Transaction Date, Owner, Ticker, Asset Name, Asset Type, Type, Amount, Comment
            _, tx_date, owner, ticker, asset, atype, ttype, amount, comment = cells[:9]
            amin, amax = _amount(amount)
            ticker = ticker.strip()
            trades.append(dict(
                owner=OWNER_MAP.get(owner, owner), asset=asset[:200],
                ticker=None if ticker in ("", "--") else ticker.upper(),
                asset_type=ASSET_TYPE_MAP.get(atype, atype[:2].upper() if atype else None),
                tx_type=TX_MAP.get(ttype, ttype.lower()), tx_date=_iso(tx_date),
                notification_date=None, amount_min=amin, amount_max=amax,
                description=("" if comment == "--" else comment)[:300],
            ))
        return trades


def sync(con, years=None) -> None:
    years = years or range(config.FIRST_YEAR, config.LAST_YEAR + 1)
    client = SenateClient()
    client.accept_terms()
    matcher = MemberMatcher(con)
    state_of = {r["bioguide_id"]: r["state"] for r in con.execute("SELECT bioguide_id, state FROM members")}
    by_name = {}
    for r in con.execute("SELECT * FROM members WHERE chamber='senate'"):
        by_name.setdefault(r["last_name"].lower(), []).append(r["state"])
    done = {r[0] for r in con.execute("SELECT doc_id FROM filings WHERE chamber='senate' AND status!='error'")}

    for year in years:
        n = 0
        for first, last, url, submitted in client.search_ptrs(f"01/01/{year}", f"12/31/{year}"):
            doc_id = url.rstrip("/").split("/")[-1]
            if doc_id in done:
                continue
            # the search result has no state; try every state a current senator with that last name holds
            bio = None
            for st in by_name.get(last.split()[-1].lower(), []) + by_name.get(last.lower(), []):
                bio = matcher.match(first, last, st)
                if bio:
                    break
            if not bio:
                continue
            is_paper = "/paper/" in url
            trades = [] if is_paper else client.parse_ptr(url)
            filed = _iso(submitted)
            for t in trades:
                t["notification_date"] = filed
            con.execute(
                "INSERT OR REPLACE INTO filings(doc_id, chamber, bioguide_id, filer_name, state_dst, filing_date, "
                "year, url, status, n_trades) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (doc_id, "senate", bio, f"{first} {last}", state_of.get(bio), filed, year, url,
                 "scanned" if is_paper else "parsed", len(trades)),
            )
            con.executemany(
                "INSERT OR IGNORE INTO trades(doc_id, bioguide_id, chamber, owner, asset, ticker, asset_type, "
                "tx_type, tx_date, notification_date, amount_min, amount_max, description) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [(doc_id, bio, "senate", t["owner"], t["asset"], t["ticker"], t["asset_type"], t["tx_type"],
                  t["tx_date"], t["notification_date"], t["amount_min"], t["amount_max"], t["description"])
                 for t in trades],
            )
            con.commit()
            n += 1
            time.sleep(0.5)
        log.info("Senate %s: %d PTRs by current senators stored", year, n)
