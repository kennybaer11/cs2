"""Collect CS2 map results from Liquipedia into the cs2 schema.

    python collect.py                  # S, A and B tier, every CS2 event so far
    python collect.py --tiers 1,2      # S and A only
    python collect.py --dry-run        # parse and count, write nothing

Tournaments that ended more than a week ago and are already stored are
skipped, and their pages are cached on disk, so a re-run reads only events
that are on or just finished. Qualifiers are left out.
"""

import argparse
import logging
import sys
from datetime import date, timedelta

from dotenv import load_dotenv

from cs2 import liquipedia as lp

log = logging.getLogger("collect")
CS2_START = date(2023, 9, 27)       # Counter-Strike 2's release: MR12 from here on
TIERS = {"S": 1, "A": 2, "B": 3, "C": 4, "D": 5}


def tournament_row(page: str, box: dict) -> dict:
    tier = (box.get("liquipediatier") or "").strip()
    tier = TIERS.get(tier[:1].upper()) if not tier.isdigit() else int(tier)
    return {"page": page, "name": (box.get("name") or page).replace("&nbsp;", " "), "tier": tier,
            "game": (box.get("game") or "").lower() or None,
            "start_date": lp.parse_day(box.get("sdate") or box.get("date")),
            "end_date": lp.parse_day(box.get("edate") or box.get("date")),
            "lan": (box.get("type") or "").lower() == "offline"}


def is_cs2(t: dict) -> bool:
    if t["game"] and t["game"] not in ("cs2",):
        return False
    return bool(t["start_date"]) and t["start_date"] >= CS2_START


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tiers", default="1,2,3")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, help="stop after this many tournaments (testing)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    load_dotenv()

    conn = None
    skip: set[str] = set()
    if not args.dry_run:
        from cs2 import db
        conn = db.connect()
        skip = db.finished_tournaments(conn)
        conn.close()            # the tier lists take minutes; Neon drops idle links

    titles = []
    for tier in map(int, args.tiers.split(",")):
        found = lp.tier_list(tier)
        log.info("tier %d: %d tournaments listed", tier, len(found))
        titles += [t for t in found if t not in titles]
    todo = [t for t in titles if t not in skip]
    log.info("%d tournaments, %d already complete, %d to read", len(titles),
             len(titles) - len(todo), len(todo))
    if args.limit:
        todo = todo[:args.limit]

    mains = lp.wikitext(todo)
    if not args.dry_run:
        conn = db.connect()
    total_m = total_maps = 0
    today = date.today()
    for n, page in enumerate(todo, 1):
        text = mains.get(page)
        if text is None:
            log.warning("%s: no page", page)
            continue
        t = tournament_row(page, lp.infobox(text))
        if not is_cs2(t) or (t["start_date"] and t["start_date"] > today):
            continue
        settled = bool(t["end_date"]) and t["end_date"] < today - timedelta(days=7)
        subs = [s for s in lp.subpages(page) if "/Qualifier" not in s and "/Open" not in s]
        texts = {page: text, **lp.wikitext(subs, cache=settled)}
        found = {}
        for title, wt in texts.items():
            for m in lp.matches(title, wt):
                found.setdefault(m["match_id"], m)
        ms = list(found.values())
        maps = sum(len(m["maps"]) for m in ms)
        total_m += len(ms)
        total_maps += maps
        log.info("[%d/%d] %s (tier %s, %s): %d matches, %d maps", n, len(todo), page, t["tier"],
                 t["start_date"], len(ms), maps)
        if conn and ms:
            db.save_tournament(conn, t, ms)
    log.info("done: %d matches, %d maps", total_m, total_maps)
    return 0


if __name__ == "__main__":
    sys.exit(main())
