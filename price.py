"""Price the stored bookmaker round lines with the model.

    python price.py              # every line of matches still to start, best EV first
    python price.py --top 40
    python price.py --store      # also write cs2.priced, which betken.cz shows

Since 11 Nov 2026 the STRENGTH of the two teams on a map comes from Betano
itself: its map-winner price (margin taken out), or failing that its 3-way
regulation result, is turned into the round share m that gives that chance of
winning the map. The model supplies the SHAPE - how a map with that m ends.
On 32,000 past maps the shape is well calibrated, while the tips that came
from the model's own team ratings lost where Betano knew more (MOUZ v G2:
model 81 % per map, Betano 66-72 %, G2 won). On 749 settled lines, Betano's
strength + the model's shape scored 0.529 log-loss v 0.533 for the model alone
and 0.540 for Betano.

A map with no Betano winner price falls back to the model's own rating (both
teams known to Liquipedia with MIN_MAPS maps behind them); those lines are
stored with m_from = 'model' for the scoreboard, and advice.py never tips them.
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
# Words that only decorate a name. Not "academy": Spirit Academy is not Team Spirit.
DROP = re.compile(r"\b(team|esports?|e-sports|gaming|clan|club|gg|cs)\b")


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


def _win(m: float, kappa: float) -> float:
    return float(model.score_dist(m, kappa)[model.T1 > model.T2].sum())


def m_for_win(q: float, kappa: float) -> float:
    """The round share m whose chance of winning the map is q."""
    from scipy.optimize import brentq
    q = min(max(q, 0.03), 0.97)
    return brentq(lambda m: _win(m, kappa) - q, 0.02, 0.98, xtol=1e-4)


def betano_strength(odds: pd.DataFrame) -> dict[int, float]:
    """{map number: team 1's chance of winning it}, margin taken out, from the
    map-winner market, else from the 3-way regulation result (team 1 / draw /
    team 2, a draw being overtime, shared in the teams' proportion)."""
    out = {}
    for n, g in odds.groupby("map_num"):
        for market in ("map_winner", "map_result"):
            s = g[g.market == market]
            p = {int(t): 1 / float(pr) for t, pr in zip(s.team, s.price)}
            if 1 in p and 2 in p:
                out[int(n)] = p[1] / (p[1] + p[2])
                break
    return out


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
        known = bool(p1 and p2) and min(ratings.n.get(p1, 0), ratings.n.get(p2, 0)) >= MIN_MAPS
        m_model = ratings.expected(p1, p2, None) if known else None
        ev_odds = odds[(odds.bookmaker == e.bookmaker) & (odds.event_id == e.event_id)]
        strength = {n: m_for_win(q, params.kappa) for n, q in betano_strength(ev_odds).items()}
        for o in ev_odds.itertuples():
            line = float(o.line)
            if o.map_num in strength:
                m, m_from = strength[o.map_num], "betano"
            elif m_model is not None:
                m, m_from = m_model, "model"
            else:
                continue
            dist = model.score_dist(m, params.kappa)
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
                         "m_from": m_from,
                         "m_model": round(m_model, 3) if m_model is not None else None,
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
    from cs2 import db
    with db.connect() as conn, conn.cursor() as cur:      # db.connect applies schema.sql
        cur.executemany("UPDATE cs2.bk_event SET team1_page=%s, team2_page=%s "
                        "WHERE bookmaker=%s AND event_id=%s",
                        [(a, b, bk, ev) for (bk, ev), (a, b) in pages.items()])
        cur.executemany(
            """INSERT INTO cs2.priced (bookmaker, event_id, market, map_num, team, side, line,
                                       price, p_model, edge, m, m_from, m_model)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (bookmaker, event_id, market, map_num, team, side, line) DO UPDATE
                  SET price=EXCLUDED.price, p_model=EXCLUDED.p_model, edge=EXCLUDED.edge,
                      m=EXCLUDED.m, m_from=EXCLUDED.m_from, m_model=EXCLUDED.m_model,
                      priced_at=now()""",
            [(r.bookmaker, r.event_id, r.market, int(r.map), int(r.team), r.side, r.line,
              r.price, r.model_p, r.ev, r.m, r.m_from,
              None if pd.isna(r.m_model) else r.m_model) for r in out.itertuples()])
    print(f"stored {len(out)} priced lines")


if __name__ == "__main__":
    sys.exit(main())
