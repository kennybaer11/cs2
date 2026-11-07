"""Settle priced lines against Liquipedia results.

    python settle.py

A bookmaker match is paired with the Liquipedia match between the same two
team pages that started within 36 hours of its kick-off, nearest first, and
the maps are taken by number. Team 1 on one side may be team 2 on the other,
so the pairing records whether they are swapped. A line on a map that was
not played is void. A match still unpaired a week after kick-off is left
alone (both teams unknown to Liquipedia, or the event not covered).
"""

import logging
import sys

from dotenv import load_dotenv

from cs2 import db

log = logging.getLogger("settle")


def outcome(market: str, team: int, side: str, line: float, r1: int, r2: int):
    """(result, actual) for one line, rounds of bookmaker team 1 and 2 given."""
    if market == "rounds":
        actual = r1 + r2
        win = actual > line if side == "over" else actual < line
    elif market == "team_rounds":
        actual = r1 if team == 1 else r2
        win = actual > line if side == "over" else actual < line
    elif market == "handicap":
        mine, theirs = (r1, r2) if team == 1 else (r2, r1)
        actual = mine - theirs
        win = actual + line > 0
    elif market == "overtime":
        actual = int(r1 + r2 > 24)
        win = bool(actual) == (side == "yes")
    else:
        return None, None
    return ("won" if win else "lost"), actual


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    load_dotenv()
    conn = db.connect()
    with conn.cursor() as cur:
        # Pair started, unpaired events with a Liquipedia match.
        cur.execute("""
            UPDATE cs2.bk_event e SET match_id = x.match_id, swapped = x.swapped
              FROM (SELECT DISTINCT ON (e.bookmaker, e.event_id) e.bookmaker, e.event_id,
                           m.match_id, (ta.page = e.team2_page) AS swapped
                      FROM cs2.bk_event e
                      JOIN cs2.match m ON m.played_at BETWEEN e.kickoff - interval '36 hours'
                                                          AND e.kickoff + interval '36 hours'
                      JOIN cs2.team ta ON ta.key = m.team1
                      JOIN cs2.team tb ON tb.key = m.team2
                     WHERE e.match_id IS NULL AND e.kickoff < now()
                       AND e.team1_page IS NOT NULL AND e.team2_page IS NOT NULL
                       AND ((ta.page, tb.page) = (e.team1_page, e.team2_page)
                         OR (ta.page, tb.page) = (e.team2_page, e.team1_page))
                     ORDER BY e.bookmaker, e.event_id, abs(extract(epoch FROM m.played_at - e.kickoff))
                   ) x
             WHERE (e.bookmaker, e.event_id) = (x.bookmaker, x.event_id)""")
        log.info("%d bookmaker matches paired with a result", cur.rowcount)
        # The opposite side's closing price, for the no-vig comparison.
        cur.execute("""
            UPDATE cs2.priced p SET other_price = o.price
              FROM cs2.priced o
             WHERE p.other_price IS NULL
               AND (o.bookmaker, o.event_id, o.market, o.map_num, o.team, o.line)
                 = (p.bookmaker, p.event_id, p.market, p.map_num, p.team, p.line)
               AND p.market IN ('rounds', 'team_rounds', 'overtime')
               AND (p.side, o.side) IN (('over','under'), ('under','over'), ('yes','no'), ('no','yes'))""")
        cur.execute("""
            UPDATE cs2.priced p SET other_price = o.price
              FROM cs2.priced o
             WHERE p.other_price IS NULL AND p.market = 'handicap' AND o.market = 'handicap'
               AND (o.bookmaker, o.event_id, o.map_num) = (p.bookmaker, p.event_id, p.map_num)
               AND o.team <> p.team AND o.line = -p.line""")
        cur.execute("""
            SELECT p.bookmaker, p.event_id, p.market, p.map_num, p.team, p.side, p.line::float,
                   e.swapped, mp.t1_rounds, mp.t2_rounds
              FROM cs2.priced p
              JOIN cs2.bk_event e USING (bookmaker, event_id)
              LEFT JOIN cs2.map mp ON mp.match_id = e.match_id AND mp.map_num = p.map_num
             WHERE p.result IS NULL AND e.match_id IS NOT NULL""")
        rows = cur.fetchall()
        done = []
        for bk, ev, market, map_num, team, side, line, swapped, a, b in rows:
            if a is None:
                res, actual = "void", None
            else:
                r1, r2 = (b, a) if swapped else (a, b)
                res, actual = outcome(market, team, side, line, r1, r2)
            if res:
                done.append((res, actual, bk, ev, market, map_num, team, side, line))
        cur.executemany("""UPDATE cs2.priced SET result=%s, actual=%s, settled_at=now()
                            WHERE (bookmaker, event_id, market, map_num, team, side, line)
                                = (%s,%s,%s,%s,%s,%s,%s)""", done)
        log.info("%d lines settled", len(done))

        # The tips, by the same rule. The lock leaves these columns writable.
        cur.execute("""
            SELECT a.bookmaker, a.event_id, a.slot, a.market, a.team, a.side, a.line::float,
                   e.swapped, mp.t1_rounds, mp.t2_rounds
              FROM cs2.advice a
              JOIN cs2.bk_event e USING (bookmaker, event_id)
              LEFT JOIN cs2.map mp ON mp.match_id = e.match_id AND mp.map_num = a.map_num
             WHERE a.result IS NULL AND e.match_id IS NOT NULL""")
        tips = []
        for bk, ev, slot, market, team, side, line, swapped, a, b in cur.fetchall():
            if a is None:
                res, actual = "void", None
            else:
                r1, r2 = (b, a) if swapped else (a, b)
                res, actual = outcome(market, team, side, line, r1, r2)
            tips.append((res, actual, bk, ev, slot))
        cur.executemany("""UPDATE cs2.advice SET result=%s, actual=%s, settled_at=now()
                            WHERE (bookmaker, event_id, slot) = (%s,%s,%s)""", tips)
        # A tip whose match has no result five days on is void.
        cur.execute("""
            UPDATE cs2.advice a SET result = 'void', settled_at = now()
              FROM cs2.bk_event e
             WHERE (e.bookmaker, e.event_id) = (a.bookmaker, a.event_id)
               AND a.result IS NULL AND e.match_id IS NULL
               AND a.kickoff < now() - interval '5 days'""")
        log.info("%d tips settled, %d voided for no result", len(tips), cur.rowcount)
    conn.commit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
