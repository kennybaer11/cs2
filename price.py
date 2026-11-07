"""Price the stored bookmaker round lines with the model.

    python price.py              # every line of matches still to start, best EV first
    python price.py --top 40
    python price.py --store      # also write cs2.priced, which betken.cz shows

Ratings are rebuilt from the whole history each run (a few seconds). A
bookmaker's team is matched to a Liquipedia team page by name; a match whose
teams are not both known, or that either team has fewer than MIN_MAPS maps
behind it, is left unpriced. Nothing is advised yet: the model first has to
beat the lines on the settled record.
"""

import argparse
import os
import re
import sys

import pandas as pd
import psycopg
from dotenv import load_dotenv

import backtest
from cs2 import model

MIN_MAPS = 15
DROP = re.compile(r"\b(team|esports?|e-sports|gaming|clan|club|gg|cs|academy)\b")


def norm(name: str) -> str:
    s = name.lower().replace("&amp;", "&")
    s = DROP.sub(" ", s)
    return re.sub(r"[^a-z0-9]+", "", s)


def team_index(conn) -> dict[str, str]:
    """normalised name -> team page, from page titles and template names."""
    idx = {}
    with conn.cursor() as cur:
        cur.execute("SELECT key, page, name FROM cs2.team WHERE page NOT LIKE '?%'")
        for key, page, name in cur.fetchall():
            for n in (page, name, key):
                if n:
                    idx.setdefault(norm(n), page)
    return idx


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=30)
    ap.add_argument("--store", action="store_true")
    args = ap.parse_args()
    load_dotenv()
    params = model.Params()
    df = backtest.load()
    ratings = model.Ratings(params)
    for _, g in df.groupby("match_id", sort=False):
        f = g.iloc[0]
        ratings.update(f.team1, f.team2, None if pd.isna(f.tier) else f.tier,
                       list(zip(g.t1_rounds, g.t2_rounds)))

    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        idx = team_index(conn)
        ev = pd.read_sql("""SELECT * FROM cs2.bk_event WHERE kickoff > now() ORDER BY kickoff""", conn)
        odds = pd.read_sql("""SELECT o.* FROM cs2.odds o JOIN cs2.bk_event e USING (bookmaker, event_id)
                               WHERE e.kickoff > now()""", conn)

    rows, unmatched, pages = [], set(), {}
    for e in ev.itertuples():
        p1, p2 = idx.get(norm(e.team1)), idx.get(norm(e.team2))
        for name, p in ((e.team1, p1), (e.team2, p2)):
            if not p:
                unmatched.add(name)
        pages[(e.bookmaker, e.event_id)] = (p1, p2)
        if not p1 or not p2 or min(ratings.n.get(p1, 0), ratings.n.get(p2, 0)) < MIN_MAPS:
            continue
        m = ratings.expected(p1, p2, None)
        dist = model.score_dist(m, params.kappa)
        for o in odds[(odds.bookmaker == e.bookmaker) & (odds.event_id == e.event_id)].itertuples():
            line = float(o.line)
            if o.market in ("handicap", "team_rounds") and o.team not in (1, 2):
                continue                    # a team line whose team is unknown
            if o.market == "rounds":
                pr = model.prob_total_over(dist, line)
                pr = pr if o.side == "over" else 1 - pr
            elif o.market == "handicap":
                pr = model.prob_handicap(dist, o.team, line)
            elif o.market == "team_rounds":
                pr = model.prob_team_over(dist, o.team, line)
                pr = pr if o.side == "over" else 1 - pr
            elif o.market == "overtime":
                pr = model.prob_overtime(dist)
                pr = pr if o.side == "yes" else 1 - pr
            else:
                continue
            price = float(o.price)
            label = {"rounds": f"total {o.side} {line}", "overtime": f"overtime {o.side}",
                     "handicap": f"{(e.team1, e.team2)[o.team - 1]} {line:+}",
                     "team_rounds": f"{(e.team1, e.team2)[o.team - 1]} rounds {o.side} {line}"
                     }[o.market]
            rows.append({"bookmaker": e.bookmaker, "event_id": e.event_id,
                         "market": o.market, "team": o.team, "side": o.side, "line": line,
                         "kickoff": e.kickoff.strftime("%d.%m %H:%M"),
                         "match": f"{e.team1} - {e.team2}", "m": round(m, 3),
                         "map": o.map_num, "bet": label, "price": price,
                         "model_p": round(pr, 3), "fair": round(1 / max(pr, 1e-6), 2),
                         "ev": round(pr * price - 1, 3)})
    out = pd.DataFrame(rows)
    if unmatched:
        print("No Liquipedia team for:", ", ".join(sorted(unmatched)))
    if out.empty:
        print("nothing priced")
        return 0
    if args.store:
        store(out, pages)
    pd.set_option("display.width", 200)
    show = ["kickoff", "match", "m", "map", "bet", "price", "model_p", "fair", "ev"]
    print(out.sort_values("ev", ascending=False)[show].head(args.top).to_string(index=False))
    return 0


def store(out: pd.DataFrame, pages: dict):
    """Rewrite the model's price on every line of matches still to start."""
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn, conn.cursor() as cur:
        cur.executemany("UPDATE cs2.bk_event SET team1_page=%s, team2_page=%s "
                        "WHERE bookmaker=%s AND event_id=%s",
                        [(a, b, bk, ev) for (bk, ev), (a, b) in pages.items()])
        cur.executemany(
            """INSERT INTO cs2.priced (bookmaker, event_id, market, map_num, team, side, line,
                                       price, p_model, edge, m)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (bookmaker, event_id, market, map_num, team, side, line) DO UPDATE
                  SET price=EXCLUDED.price, p_model=EXCLUDED.p_model, edge=EXCLUDED.edge,
                      m=EXCLUDED.m, priced_at=now()""",
            [(r.bookmaker, r.event_id, r.market, int(r.map), int(r.team), r.side, r.line,
              r.price, r.model_p, r.ev, r.m) for r in out.itertuples()])
    print(f"stored {len(out)} priced lines")


if __name__ == "__main__":
    sys.exit(main())
