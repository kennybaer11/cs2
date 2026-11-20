"""Where should a map's team strength come from? Every settled Betano line,
priced with the model's shape and the strength taken from the model's own team
rating, from Betano's map price (what price.py does since 11 Nov 2026), or a
blend of the two - against Betano's own price with its margin taken out.

    python compare.py

Prints log-loss per market and blend weight, bootstrap intervals by match
(lines of one match are correlated), the map winner alone, and flat stakes on
the own-rating edges under the tip rules. Only what was known before kick-off
is used: m_model is stored with each line before the match starts.

First run, 20 Nov 2026 (6,660 lines, 42 matches, 103 maps): Betano strength
0.5518, Betano no-vig 0.5541, own rating 0.5665; every blend weight on the own
rating was worse; map winner own 0.681 v Betano 0.656; own-rating tips 38 bets,
5 won, -59 %.
"""
import os
import sys

import numpy as np
import pandas as pd
import psycopg
from dotenv import load_dotenv

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
load_dotenv(os.path.join(HERE, ".env"))
from cs2 import model                      # noqa: E402
from price import betano_strength, m_for_win  # noqa: E402

K = model.Params().kappa
rng = np.random.default_rng(1)

with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
    pr = pd.read_sql("""
        SELECT p.*, e.kickoff, e.team1, e.team2, e.match_id, e.swapped
          FROM cs2.priced p JOIN cs2.bk_event e USING (bookmaker, event_id)
         WHERE p.bookmaker='betano' AND p.result IN ('won','lost')""", conn)
    od = pd.read_sql("""
        SELECT o.* FROM cs2.odds o
         WHERE o.bookmaker='betano' AND o.market IN ('map_winner','map_result')
           AND o.event_id IN (SELECT event_id FROM cs2.priced WHERE result IS NOT NULL)""", conn)
    maps = pd.read_sql("""SELECT match_id, map_num, t1_rounds, t2_rounds FROM cs2.map
                           WHERE match_id IN (SELECT match_id FROM cs2.bk_event WHERE match_id IS NOT NULL)""", conn)

# Betano's strength per (event, map): chance team 1 wins the map, margin out.
q = {}
for ev, g in od.groupby("event_id"):
    for n, p in betano_strength(g).items():
        q[(ev, n)] = p

# The model's own rating: m_model where stored (since 11 Nov); before that the
# line was priced with the model's own m, stored in m.
pr["m_own"] = pr.m_model.where(pr.m_model.notna(), pr.m.where(pr.m_from.isna() | (pr.m_from == "model")))
pr["q"] = [q.get((e, n)) for e, n in zip(pr.event_id, pr.map_num)]
d = pr.dropna(subset=["m_own", "q", "other_price"]).copy()
d = d[d.market.isin(["rounds", "handicap", "team_rounds", "overtime"])]
d["m_own"] = d.m_own.astype(float)
_mcache = {}
def mfw(x):
    k = round(x, 4)
    if k not in _mcache:
        _mcache[k] = m_for_win(k, K)
    return _mcache[k]
d["m_bet"] = d.q.astype(float).map(mfw)
for c in ("price", "other_price", "line"):
    d[c] = d[c].astype(float)
d["y"] = (d.result == "won").astype(int)
d["p_nv"] = (1 / d.price) / (1 / d.price + 1 / d.other_price)


def prob(row, m):
    dist = model.score_dist(m, K)
    if row.market == "rounds":
        p = model.prob_total_over(dist, row.line)
        return p if row.side == "over" else 1 - p
    if row.market == "handicap":
        return model.prob_handicap(dist, int(row.team), row.line)
    if row.market == "team_rounds":
        p = model.prob_team_over(dist, int(row.team), row.line)
        return p if row.side == "over" else 1 - p
    p = model.prob_overtime(dist)
    return p if row.side == "yes" else 1 - p


def logit(x):
    return np.log(x / (1 - x))


def ll(p, y):
    p = np.clip(p, 1e-4, 1 - 1e-4)
    return -(y * np.log(p) + (1 - y) * np.log(1 - p))


W = [0.0, 0.25, 0.5, 0.75, 1.0]           # weight on the model's own rating, in logit(m)
for w in W:
    mm = 1 / (1 + np.exp(-(w * logit(d.m_own) + (1 - w) * logit(d.m_bet))))
    d[f"p_w{w}"] = [prob(r, m) for r, m in zip(d.itertuples(), mm)]
    d[f"ll_w{w}"] = ll(d[f"p_w{w}"], d.y)
d["ll_nv"] = ll(d.p_nv, d.y)

print(f"settled two-sided lines with both strengths: {len(d)} on {d.event_id.nunique()} matches, "
      f"{d.groupby(['event_id','map_num']).ngroups} maps\n")
cols = [f"ll_w{w}" for w in W] + ["ll_nv"]
names = {f"ll_w{w}": f"own {int(w*100)}%" for w in W} | {"ll_nv": "Betano no-vig"}
t = d.groupby("market")[cols].mean()
t.loc["ALL"] = d[cols].mean()
t["n"] = d.groupby("market").size()
t.loc["ALL", "n"] = len(d)
print("log-loss (own X% = weight on the model's own rating; 0% = current price.py)")
print(t.rename(columns=names).round(4).to_string(), "\n")

# Bootstrap by match: lines of one match are correlated.
evs = d.event_id.unique()
per = d.groupby("event_id")[cols].agg(["sum"]).droplevel(1, axis=1)
cnt = d.groupby("event_id").size()
def boot(a, b, B=4000):
    out = []
    for _ in range(B):
        s = rng.choice(evs, len(evs))
        out.append((per.loc[s, a].sum() - per.loc[s, b].sum()) / cnt.loc[s].sum())
    return np.percentile(out, [2.5, 50, 97.5])
for a, b in (("ll_w1.0", "ll_w0.0"), ("ll_w1.0", "ll_nv"), ("ll_w0.0", "ll_nv"), ("ll_w0.25", "ll_w0.0")):
    lo, mid, hi = boot(a, b)
    print(f"{names[a]:>14} - {names[b]:<14} {mid:+.4f}  95% [{lo:+.4f}, {hi:+.4f}]  (negative = first is better)")

# Strength alone: the map winner. Own rating's win chance v Betano's, on maps played.
mw = d.groupby(["event_id", "map_num"]).agg(m_own=("m_own", "first"), q=("q", "first"),
                                           match_id=("match_id", "first"), swapped=("swapped", "first")).reset_index()
mw = mw.merge(maps, on=["match_id", "map_num"], how="inner")
mw["t1_won"] = np.where(mw.swapped.fillna(False), mw.t2_rounds > mw.t1_rounds, mw.t1_rounds > mw.t2_rounds).astype(int)
mw["p_own"] = [float(model.score_dist(m, K)[model.T1 > model.T2].sum()) for m in mw.m_own]
mw["q"] = mw.q.astype(float)
print(f"\nmap winner on {len(mw)} played maps:  own rating {ll(mw.p_own, mw.t1_won).mean():.4f}   "
      f"Betano {ll(mw.q, mw.t1_won).mean():.4f}   (coin 0.6931)")
mw["gap"] = (mw.p_own - mw.q).abs()
big = mw[mw.gap >= 0.10]
print(f"maps where they differ by 10+ points: {len(big)};  own {ll(big.p_own, big.t1_won).mean():.4f}  "
      f"Betano {ll(big.q, big.t1_won).mean():.4f};  the side own rating favoured won "
      f"{((big.p_own > big.q) == big.t1_won.astype(bool)).mean():.0%}")

# Flat stakes: the advice rules on own-rating prices (edge >= 10%, odds 1.40-4.50, best line per map).
for w in (1.0, 0.25):
    b = d.assign(edge=d[f"p_w{w}"] * d.price - 1)
    b = b[(b.edge >= 0.10) & b.price.between(1.40, 4.50)]
    b = b.sort_values("edge", ascending=False).groupby(["event_id", "map_num"]).head(1)
    pl = np.where(b.y == 1, b.price - 1, -1.0)
    print(f"flat 1u, own {int(w*100)}%: {len(b)} bets, {b.y.sum()} won, P/L {pl.sum():+.2f}u, ROI {pl.mean() if len(b) else 0:+.1%}")
