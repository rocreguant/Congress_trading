"""Currently serving members of Congress (from the public unitedstates/congress-legislators dataset)."""
from __future__ import annotations

import re
import unicodedata

import requests

from . import config


def norm(name: str | None) -> str:
    """Lower-case, strip accents and punctuation: 'Sánchez-Pérez Jr.' -> 'sanchez perez jr'."""
    if not name:
        return ""
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z ]+", " ", name.lower()).strip()


def fetch_current_members(con) -> int:
    resp = requests.get(config.LEGISLATORS_URL, timeout=60, headers={"User-Agent": config.USER_AGENT})
    resp.raise_for_status()
    rows = []
    for p in resp.json():
        term = p["terms"][-1]
        name = p["name"]
        rows.append(
            (
                p["id"]["bioguide"],
                name.get("first"),
                name.get("last"),
                name.get("official_full") or f"{name.get('first')} {name.get('last')}",
                "senate" if term["type"] == "sen" else "house",
                term["state"],
                term.get("district"),
                term.get("party"),
            )
        )
    con.executemany("INSERT OR REPLACE INTO members VALUES (?,?,?,?,?,?,?,?)", rows)
    con.commit()
    return len(rows)


class MemberMatcher:
    """Match a disclosure filer (first/last name + state) to a current member's bioguide id.

    Members who moved from the House to the Senate still match on state + last name,
    so their earlier House trades are attributed to them too.
    """

    def __init__(self, con):
        self.by_state_last: dict[tuple[str, str], list[dict]] = {}
        for r in con.execute("SELECT * FROM members"):
            r = dict(r)
            last = norm(r["last_name"])
            keys = {last, last.split()[-1]} | ({last.replace(" ", "")} if " " in last else set())
            for k in keys:
                self.by_state_last.setdefault((r["state"], k), []).append(r)

    def match(self, first: str, last: str, state: str) -> str | None:
        last_n = norm(last)
        # strip suffixes that sometimes end up in the last-name field
        last_n = re.sub(r"\b(jr|sr|ii|iii|iv)\b", "", last_n).strip()
        cands = []
        for key in (last_n, last_n.split()[-1] if last_n else "", last_n.replace(" ", "")):
            cands = self.by_state_last.get((state, key), [])
            if cands:
                break
        if not cands:
            return None
        uniq = {c["bioguide_id"]: c for c in cands}
        if len(uniq) == 1:
            return next(iter(uniq))
        first_n = norm(first)
        for c in uniq.values():
            cf = norm(c["first_name"])
            if cf and first_n and (first_n.startswith(cf) or cf.startswith(first_n.split()[0])):
                return c["bioguide_id"]
        return None
