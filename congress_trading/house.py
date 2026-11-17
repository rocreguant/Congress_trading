"""House of Representatives Periodic Transaction Reports (PTRs).

Source: the official Clerk of the House disclosure site, which is free and has no
API key or click-through agreement:
  * a yearly ZIP index of every filing: {year}FD.zip  (FilingType 'P' = PTR)
  * one PDF per PTR: ptr-pdfs/{year}/{DocID}.pdf

Electronically filed PTRs (DocID starting with '2') are text PDFs and are parsed
here. Paper filings (DocID starting with '8') are scanned images and are recorded
with status 'scanned' but not parsed (they would need OCR).
"""
from __future__ import annotations

import io
import logging
import re
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import pdfplumber
import requests

from . import config
from .members import MemberMatcher

log = logging.getLogger(__name__)

_session = requests.Session()
_session.headers["User-Agent"] = config.USER_AGENT

CORE_RE = re.compile(
    r"(?:^|\s)(?P<type>P|S \(partial\)|S|E)\s+"
    r"(?P<d1>\d{1,2}/\d{1,2}/\d{4})\s+(?P<d2>\d{1,2}/\d{1,2}/\d{4})\s*(?P<amt>.*)$"
)
# Older PDFs use a small-caps font that extracts as mixed case: "(DIs)", "[oT]".
TICKER_RE = re.compile(r"\(([A-Za-z][A-Za-z0-9.\-/]{0,6})\)")
ATYPE_RE = re.compile(r"\[([A-Za-z]{2})\]")
NOT_TICKERS = {"ADR", "ADS", "USA", "NEW", "THE", "LP", "LLC", "ETF", "REIT", "CLASS", "INC", "CORP", "NYSE", "PLC", "ETN"}
META_RE = re.compile(r"^[A-Za-z\x00 ]{1,30}:")  # 'FILINg STATUS: New', 'DESCRIPTIoN: ...'
MONEY_RE = re.compile(r"\$([\d,]+(?:\.\d+)?)")
OWNERS = {"SP", "JT", "DC"}
HEADER_HINTS = ("iD owner", "ID Owner", "type Date", "Type Date", "$200?", "tranSaction", "T  ")
TX_MAP = {"P": "buy", "S": "sell", "S (partial)": "sell", "E": "exchange"}


def _iso(d: str) -> str | None:
    try:
        return datetime.strptime(d, "%m/%d/%Y").date().isoformat()
    except ValueError:
        return None


def _money(s: str) -> float:
    return float(s.replace(",", ""))


# --------------------------------------------------------------------------- index


def fetch_index(year: int) -> list[dict]:
    """Return every PTR row in the yearly House index."""
    resp = _session.get(config.HOUSE_INDEX_URL.format(year=year), timeout=120)
    resp.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        txt = zf.read(f"{year}FD.txt").decode("utf-8-sig", errors="replace")
    rows = []
    lines = [l.rstrip("\r") for l in txt.splitlines()]
    header = lines[0].split("\t")
    for line in lines[1:]:
        parts = line.split("\t")
        if len(parts) < len(header):
            continue
        rec = dict(zip(header, parts))
        if rec["FilingType"] == "P":
            rows.append(rec)
    return rows


# --------------------------------------------------------------------------- parsing


def _is_header(line: str) -> bool:
    return any(h in line for h in HEADER_HINTS) or line.startswith("Filing ID")


def parse_ptr_text(text: str) -> list[dict]:
    """Parse the text of an electronically filed House PTR into trade dicts."""
    lines = [l.replace("\x00", " ").strip() for l in text.splitlines()]
    trades = []
    for i, line in enumerate(lines):
        m = CORE_RE.search(line)
        if not m or META_RE.match(line):
            continue
        prefix = line[: m.start()].strip()

        # continuation lines after the core line (wrapped asset name, [ST], max amount)
        after = []
        for nxt in lines[i + 1 : i + 4]:
            if not nxt or META_RE.match(nxt) or CORE_RE.search(nxt) or nxt.startswith("*") or _is_header(nxt):
                break
            after.append(nxt)
        # lines before the core line belonging to the same row (wrapped asset name)
        before = []
        for prv in reversed(lines[max(0, i - 2) : i]):
            if not prv or META_RE.match(prv) or CORE_RE.search(prv) or _is_header(prv):
                break
            before.insert(0, prv)

        first_tokens = (before[0] if before else prefix).split()
        owner = first_tokens[0] if first_tokens and first_tokens[0] in OWNERS else "self"

        asset_text = " ".join(before + [prefix] + [a for a in after if not a.startswith("$")])
        asset_text = re.sub(r"^(SP|JT|DC)\s+", "", asset_text)
        asset_text = re.sub(r"\$[\d,]+(?:\.\d+)?|\bgfedc\w*", "", asset_text).strip()

        ticker = None
        for chunk in ([prefix], after, before):  # nearest to the core line wins
            for c in chunk:
                found = [f.upper() for f in TICKER_RE.findall(c) if f.upper() not in NOT_TICKERS]
                if found:
                    ticker = found[-1]
                    break
            if ticker:
                break
        atype = None
        for c in [prefix] + after + before:
            t = ATYPE_RE.search(c)
            if t:
                atype = t.group(1).upper()
                break

        amt_text = m.group("amt") + " " + " ".join(after)
        vals = [_money(v) for v in MONEY_RE.findall(amt_text)]
        if not vals:
            amin = amax = None
        elif "-" in m.group("amt") and len(vals) >= 2:
            amin, amax = vals[0], vals[1]
        elif "over" in m.group("amt").lower():
            amin, amax = vals[0], vals[0] * 2
        else:
            amin = amax = vals[0]

        desc = ""
        for nxt in lines[i + 1 : i + 6]:
            if CORE_RE.search(nxt):
                break
            if nxt.upper().startswith("DESCRIPTION") or nxt.startswith("D "):
                desc = nxt.split(":", 1)[-1].strip()

        if ticker:
            ticker = ticker.replace("/", ".").strip(".")
        trades.append(
            dict(
                owner=owner,
                asset=re.sub(r"\s*\[[A-Za-z]{2}\]", "", asset_text)[:200],
                ticker=ticker,
                asset_type=atype,
                tx_type=TX_MAP[m.group("type")],
                tx_date=_iso(m.group("d1")),
                notification_date=_iso(m.group("d2")),
                amount_min=amin,
                amount_max=amax,
                description=desc[:300],
            )
        )
    return trades


def _download_pdf(year: int, doc_id: str) -> bytes:
    path = config.CACHE_DIR / "house" / str(year) / f"{doc_id}.pdf"
    if path.exists() and path.stat().st_size > 0:
        return path.read_bytes()
    resp = _session.get(config.HOUSE_PTR_URL.format(year=year, doc_id=doc_id), timeout=120)
    resp.raise_for_status()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(resp.content)
    return resp.content


def _pdf_text(data: bytes) -> str:
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        return "\n".join(page.extract_text() or "" for page in pdf.pages)


def _process(year: int, doc_id: str) -> tuple[str, list[dict]]:
    if doc_id.startswith("8"):
        return "scanned", []
    text = _pdf_text(_download_pdf(year, doc_id))
    if len(text.strip()) < 50:
        return "scanned", []
    return "parsed", parse_ptr_text(text)


# --------------------------------------------------------------------------- driver


def sync(con, years=None, workers: int = 4, only_current: bool = True) -> None:
    """Download and parse every House PTR for the given years into the database."""
    years = years or range(config.FIRST_YEAR, config.LAST_YEAR + 1)
    matcher = MemberMatcher(con)
    done = {r[0] for r in con.execute("SELECT doc_id FROM filings WHERE chamber='house' AND status!='error'")}

    jobs = []
    for year in years:
        rows = fetch_index(year)
        matched = 0
        for r in rows:
            state = r["StateDst"][:2]
            bio = matcher.match(r["First"], r["Last"], state)
            if only_current and not bio:
                continue
            matched += 1
            if r["DocID"] in done:
                continue
            fd = r["FilingDate"]
            con.execute(
                "INSERT OR REPLACE INTO filings(doc_id, chamber, bioguide_id, filer_name, state_dst, "
                "filing_date, year, url, status) VALUES (?,?,?,?,?,?,?,?, 'pending')",
                (
                    r["DocID"], "house", bio, f"{r['First']} {r['Last']}".strip(), r["StateDst"],
                    _iso(fd) if fd else None, year,
                    config.HOUSE_PTR_URL.format(year=year, doc_id=r["DocID"]),
                ),
            )
            jobs.append((year, r["DocID"], bio))
        con.commit()
        log.info("House %s: %d PTRs in index, %d by current members", year, len(rows), matched)

    log.info("House: %d new PTRs to download/parse", len(jobs))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(_process, y, d): (y, d, b) for y, d, b in jobs}
        for n, fut in enumerate(as_completed(futs), 1):
            year, doc_id, bio = futs[fut]
            try:
                status, trades = fut.result()
            except Exception as exc:  # network or PDF error: keep going, retry next run
                log.warning("House %s/%s failed: %s", year, doc_id, exc)
                con.execute("UPDATE filings SET status='error' WHERE doc_id=?", (doc_id,))
                continue
            con.executemany(
                "INSERT OR IGNORE INTO trades(doc_id, bioguide_id, chamber, owner, asset, ticker, asset_type, "
                "tx_type, tx_date, notification_date, amount_min, amount_max, description) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (doc_id, bio, "house", t["owner"], t["asset"], t["ticker"], t["asset_type"], t["tx_type"],
                     t["tx_date"], t["notification_date"], t["amount_min"], t["amount_max"], t["description"])
                    for t in trades
                ],
            )
            con.execute("UPDATE filings SET status=?, n_trades=? WHERE doc_id=?", (status, len(trades), doc_id))
            if n % 50 == 0:
                con.commit()
                log.info("House: %d/%d PTRs processed", n, len(jobs))
    con.commit()
