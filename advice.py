"""Write cs2.advice - the tips - from the model's prices (cs2.priced).

    python advice.py            # after price.py --store

The rules, as for the tennis tips:

  - the model's chance is blended 3:1 with Betano's own (margin taken out),
    so a tip needs the model AND some room against the market;
  - a tip needs EDGE (+10%) on that blended chance, odds between MIN_ODDS and
    MAX_ODDS, and a price on both sides of the line (to take the margin out).
    Overtime is left out: its "yes" is a 6.0 long shot;
  - at most one tip per map: the line with the best blended edge;
  - stake: quarter Kelly on the chance blended half and half with the
    bookmaker's, scaled down above odds of 3.0, 1-10 tenths of a unit, and the
    tips on one match share the largest single stake between them.

Until kick-off a tip is rewritten each run, and one no longer advised is kept
with backed = false (withdrawn). From kick-off the database refuses changes.
"""

import os
import subprocess
import sys
from collections import defaultdict

import psycopg
from dotenv import load_dotenv

EDGE = 0.10
MIN_ODDS, MAX_ODDS = 1.40, 4.50
MARKETS = ["rounds", "handicap", "team_rounds"]
BLEND = 0.75


def code_version():
    try:
        return subprocess.run(["git", "rev-parse", "--short=12", "HEAD"], capture_output=True,
                              text=True, check=True,
                              cwd=os.path.dirname(os.path.abspath(__file__))).stdout.strip() or None
    except Exception:
        return None


def stake(p: float, odds: float) -> int:
    p = (p + 1 / odds) / 2
    b = odds - 1
    kelly = (p * odds - 1) / b if b > 0 else 0.0
    quarter = max(kelly, 0.0) / 4 * min(1.0, 3.0 / odds)
    return max(1, min(10, round(quarter * 100)))


CANDIDATES = """
    SELECT p.bookmaker, p.event_id, e.kickoff, e.league, e.team1, e.team2,
           p.map_num, p.market, p.team, p.side, p.line::float, p.price::float,
           p.p_model::float, o.price::float AS other
      FROM cs2.priced p
      JOIN cs2.bk_event e USING (bookmaker, event_id)
      LEFT JOIN cs2.priced o
        ON (o.bookmaker, o.event_id, o.market, o.map_num) = (p.bookmaker, p.event_id, p.market, p.map_num)
       AND ((p.market <> 'handicap' AND o.team = p.team AND o.line = p.line AND o.side <> p.side)
         OR (p.market = 'handicap' AND o.team <> p.team AND o.line = -p.line))
     WHERE e.kickoff > now() AND p.market = ANY(%s)"""


def main():
    load_dotenv()
    version = code_version()
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn, conn.cursor() as cur:
        cur.execute(CANDIDATES, (MARKETS,))
        best, events = {}, {}
        for (bk, ev, kickoff, league, t1, t2, map_num, market, team, side, line, price,
             p_raw, other) in cur.fetchall():
            events[(bk, ev)] = (kickoff, league, t1, t2)
            if not other or not (MIN_ODDS <= price <= MAX_ODDS):
                continue
            q_book = (1 / price) / (1 / price + 1 / other)
            p = BLEND * p_raw + (1 - BLEND) * q_book
            edge = p * price - 1
            if edge < EDGE:
                continue
            k = (bk, ev, f"map{map_num}")
            if k not in best or edge > best[k]["edge"]:
                best[k] = {"map_num": map_num, "market": market, "team": team, "side": side,
                           "line": line, "odds": price, "p": p, "p_raw": p_raw, "edge": edge}

        # Stakes: each tip's own, then one budget per match shared in proportion.
        by_match = defaultdict(list)
        for k, t in best.items():
            t["own"] = stake(t["p"], t["odds"])
            by_match[k[:2]].append(t)
        for tips in by_match.values():
            budget, want = max(t["own"] for t in tips), sum(t["own"] for t in tips)
            for t in tips:
                t["rating"] = max(1, round(t["own"] * budget / want))

        for (bk, ev, slot), t in best.items():
            kickoff, league, t1, t2 = events[(bk, ev)]
            cur.execute("""
                INSERT INTO cs2.advice (bookmaker, event_id, slot, kickoff, league, team1, team2,
                                        map_num, market, team, side, line, odds, p_model, p_raw,
                                        edge, rating, backed, code_version)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,true,%s)
                ON CONFLICT (bookmaker, event_id, slot) DO UPDATE SET
                  kickoff=EXCLUDED.kickoff, market=EXCLUDED.market, team=EXCLUDED.team,
                  side=EXCLUDED.side, line=EXCLUDED.line, odds=EXCLUDED.odds,
                  p_model=EXCLUDED.p_model, p_raw=EXCLUDED.p_raw, edge=EXCLUDED.edge,
                  rating=EXCLUDED.rating, backed=true, code_version=EXCLUDED.code_version,
                  advised_at=now()
                WHERE cs2.advice.kickoff > now() AND NOT cs2.advice.placed""",
                        (bk, ev, slot, kickoff, league, t1, t2, t["map_num"], t["market"],
                         t["team"], t["side"], t["line"], t["odds"], t["p"], t["p_raw"],
                         t["edge"], t["rating"], version))

        # Advised before, not now: keep the last advice, marked withdrawn.
        keep = [f"{bk}|{ev}|{slot}" for bk, ev, slot in best]
        cur.execute("""
            UPDATE cs2.advice SET backed = false, advised_at = now()
             WHERE backed AND kickoff > now() AND NOT placed
               AND NOT (bookmaker || '|' || event_id || '|' || slot = ANY(%s))""", (keep,))
        withdrawn = cur.rowcount
    print(f"{len(best)} tips on {len(by_match)} matches, {withdrawn} withdrawn")
    return 0


if __name__ == "__main__":
    sys.exit(main())
