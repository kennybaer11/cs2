"""Price Chance.cz CS2 lines with the map-shape model and show where Chance is off.

    python chance_check.py chance.json

chance.json: the rows a chance.cz tab collects (see chance_tab.js), one per
priced cell: [match id, start, competition, home, away, table, box, cell, odd].

Each map's team strength comes from a bookmaker's own map-winner price (margin
taken out), and the model only says how a map with that strength ends - the
same split price.py uses for Betano. Two strengths are tried:

  chance   Chance's own "Vítěz N. mapy" price: is Chance consistent with itself?
  betano   Betano's map-winner price for the same match, where Betano has it:
           does Chance disagree with the sharper book?

Priced: round handicaps, total rounds, exact map scores, and "map winner and
total rounds" combos. Nothing is stored or advised; this is the check.
"""

import json
import os
import re
import sys
import unicodedata
import warnings

import pandas as pd
import psycopg
from dotenv import load_dotenv

from cs2 import model
from price import m_for_win, norm

warnings.filterwarnings("ignore")
KAPPA = model.Params().kappa
MAP_N = re.compile(r"(\d)\. map")


def load_rows(path):
    text = open(path, encoding="utf-8").read()
    data = json.loads(text)
    if isinstance(data, list) and data and isinstance(data[0], dict):      # tool-result wrapper
        s = data[0]["text"]
        s = s[:s.rfind('"') + 1] if not s.endswith('"') else s
        data = json.loads(json.loads(s))
    return data


def side_of(name, home, away):
    """1 for the home team, 2 for the away one, by the start of the cell text."""
    n = name.lower()
    hits = [i for i, t in ((1, home), (2, away)) if n.startswith(t.lower())]
    return hits[0] if len(hits) == 1 else None


def price_cell(dist, table, cell, home, away):
    """Model probability of one Chance cell, or None if not priced."""
    if table.startswith("Handicap (kola"):
        m = re.match(r"(.*) ([+-]\d+\.5)$", cell)
        team = m and side_of(m[1], home, away)
        return model.prob_handicap(dist, team, float(m[2])) if team else None
    if table.startswith("Počet kol v"):
        m = re.match(r"(Méně|Více) než (\d+\.5)$", cell)
        if not m:
            return None
        p = model.prob_total_over(dist, float(m[2]))
        return p if m[1] == "Více" else 1 - p
    if table.startswith("Přesný výsledek"):
        m = re.match(r"(\d+):(\d+)$", cell)
        return model.prob_exact(dist, f"{m[1]}-{m[2]}") if m else None
    if " a počet kol" in table:
        m = re.match(r"(.*) a (méně|více) než (\d+\.5)$", cell, re.I)
        team = m and side_of(m[1], home, away)
        if not team:
            return None
        win = (model.T1 > model.T2) if team == 1 else (model.T2 > model.T1)
        over = model.TOTAL > float(m[3])
        return float(dist[win & (over if m[2].lower() == "více" else ~over)].sum())
    return None


def betano_strength(conn):
    """{(normalised home, normalised away, map): P(Betano team 1 wins)} plus the
    Betano team order, for matches still to start."""
    out = {}
    rows = conn.execute("""
        SELECT e.team1, e.team2, o.map_num, o.team, o.price::float
          FROM cs2.odds o JOIN cs2.bk_event e USING (bookmaker, event_id)
         WHERE e.kickoff > now() - interval '3 hours' AND o.market = 'map_winner'""").fetchall()
    tmp = {}
    for t1, t2, n, team, price in rows:
        tmp.setdefault((t1, t2, n), {})[team] = 1 / price
    for (t1, t2, n), p in tmp.items():
        if 1 in p and 2 in p:
            out[(norm(t1), norm(t2), n)] = p[1] / (p[1] + p[2])
    return out


def main():
    load_dotenv()
    rows = pd.DataFrame(load_rows(sys.argv[1]),
                        columns=["id", "start", "comp", "home", "away", "table", "box", "cell", "odd"])
    rows["map"] = rows.table.str.extract(MAP_N).astype(float)
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        bet = betano_strength(conn)
    out = []
    same = lambda x, y: x == y or (min(len(x), len(y)) >= 3 and (x in y or y in x))
    for (mid, n), g in rows[rows["map"].notna()].groupby(["id", "map"]):
        n = int(n)
        home, away = g.home.iloc[0], g.away.iloc[0]
        win = g[g.table == f"Vítěz {n}. mapy"]
        p = {side_of(c, home, away): 1 / o for c, o in zip(win.cell, win.odd)}
        strengths = {}
        if 1 in p and 2 in p:
            strengths["chance"] = p[1] / (p[1] + p[2])
        h, a = norm(home), norm(away)
        for (bh, ba, bn), q in bet.items():
            if bn != n:
                continue
            if same(bh, h) and same(ba, a):
                strengths["betano"] = q
            elif same(bh, a) and same(ba, h):
                strengths["betano"] = 1 - q
        for src, q in strengths.items():
            dist = model.score_dist(m_for_win(q, KAPPA), KAPPA)
            for r in g.itertuples():
                if r.table.startswith("Vítěz") and " a " not in r.table:
                    continue
                pr = price_cell(dist, r.table, r.cell, home, away)
                if pr is None:
                    continue
                out.append({"start": r.start[5:16].replace("T", " "), "match": f"{home} v {away}",
                            "map": n, "strength": src, "p_win1": round(q, 3), "market": r.table,
                            "bet": r.cell, "odd": r.odd, "model": round(pr, 3),
                            "edge": round(pr * r.odd - 1, 3)})
    d = pd.DataFrame(out)
    pd.set_option("display.width", 220); pd.set_option("display.max_colwidth", 40)
    print(f"{rows.id.nunique()} Chance matches, {len(d)} priced cells")
    for src in ("chance", "betano"):
        s = d[d.strength == src]
        if s.empty:
            continue
        print(f"\n== strength from {src}: {s.match.nunique()} matches, {len(s)} cells; "
              f"edge >= 10%: {(s.edge >= .10).sum()}, >= 5%: {(s.edge >= .05).sum()}")
        print(s.groupby(s.market.str.replace(r"\d\. map\w*", "N. mapě", regex=True))
               .edge.describe()[["count", "mean", "50%", "max"]].round(3).to_string())
        print(s[(s.odd <= 5)].sort_values("edge", ascending=False).head(15)
               [["start", "match", "map", "market", "bet", "odd", "model", "edge"]].to_string(index=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
