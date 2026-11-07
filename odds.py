"""Store Betano's CS2 round lines.

    python odds.py --dry-run     # print what would be stored
    python odds.py               # store every round line of matches still to start

Lines stop changing at kickoff: a started match is no longer listed, so its
last stored price is the closing one.
"""

import argparse
import logging
import sys
from collections import Counter

from dotenv import load_dotenv

from cs2 import betano

log = logging.getLogger("odds")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    load_dotenv()
    conn = None
    if not args.dry_run:
        from cs2 import db
        conn = db.connect()
    try:
        events = [e for lg in betano.leagues() for e in betano.league_events(lg)]
    except betano.Blocked as e:
        log.error("Betano refused: %s", e)
        return 1
    log.info("%d CS2 matches to start", len(events))
    for e in events:
        try:
            rows = betano.event_odds(e)
        except betano.Blocked as err:
            log.error("Betano refused: %s - stopping", err)
            return 1
        kinds = Counter(r["market"] for r in rows)
        log.info("%s  %s - %s  [%s]  %s", e["kickoff"].strftime("%d.%m %H:%M"), e["team1"],
                 e["team2"], e["league"], dict(kinds) or "no round lines")
        if conn and rows:
            db.save_odds(conn, "betano", e, rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
