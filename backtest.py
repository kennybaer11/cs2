"""Walk-forward test of the rounds model against a no-information baseline.

    python backtest.py --start 2025-01-01
    python backtest.py --start 2025-01-01 --k 0.01 --kappa 50

Every map from --start on is predicted from earlier matches only, all maps of
a match before any of them updates the ratings. Scored three ways:

    exact     log-loss of the map's exact score (overtime scores pooled as 'OT')
    total     log-loss of total rounds over 21.5 (Betano's most common middle line)
    winner    log-loss of who won the map

The baseline is the same score distribution with no team information: m = 0.5
and the same spread. It is what a rating has to beat to be worth anything.
"""

import argparse
import os
import sys

import numpy as np
import pandas as pd
import psycopg
from dotenv import load_dotenv

from cs2 import model


def load() -> pd.DataFrame:
    with psycopg.connect(os.environ["DATABASE_URL"]) as c:
        return pd.read_sql("""
            SELECT m.match_id, m.played_at,
                   coalesce(a.page, m.team1) AS team1, coalesce(b.page, m.team2) AS team2,
                   t.tier, t.lan, p.map_num, p.map_name, p.t1_rounds, p.t2_rounds, p.ot_rounds
              FROM cs2.map p JOIN cs2.match m USING (match_id)
              JOIN cs2.tournament t ON t.page = m.tournament
              LEFT JOIN cs2.team a ON a.key = m.team1
              LEFT JOIN cs2.team b ON b.key = m.team2
             WHERE m.played_at IS NOT NULL
             ORDER BY m.played_at, m.match_id, p.map_num""", c)


def score_index(a: int, b: int) -> int | None:
    """Regulation scores map to their own cell, overtime to None (scored pooled)."""
    return model.INDEX.get((a, b)) if max(a, b) == 13 else None


def map_logloss(dist: np.ndarray, a: int, b: int) -> float:
    i = score_index(a, b)
    p = dist[i] if i is not None else dist[model.TOTAL > 24].sum()
    return -np.log(max(p, 1e-9))


def run(df: pd.DataFrame, params: model.Params, start: str, line: float = 21.5):
    ratings = model.Ratings(params)
    rows = []
    start_ts = pd.Timestamp(start, tz="UTC")
    for mid, g in df.groupby("match_id", sort=False):
        first = g.iloc[0]
        t1, t2, tier = first.team1, first.team2, first.tier
        if pd.isna(tier):
            tier = None
        if first.played_at >= start_ts:
            seen = min(ratings.n.get(t1, 0), ratings.n.get(t2, 0))
            m = ratings.expected(t1, t2, tier)
            dist = model.score_dist(m, params.kappa)
            base = model.score_dist(0.5, params.kappa)
            for r in g.itertuples():
                a, b = r.t1_rounds, r.t2_rounds
                over = (a + b) > line
                po, bo = model.prob_total_over(dist, line), model.prob_total_over(base, line)
                pw = float(dist[model.T1 > model.T2].sum())
                rows.append({"played_at": r.played_at, "tier": tier, "seen": seen, "m": m,
                             "exact": map_logloss(dist, a, b), "exact_base": map_logloss(base, a, b),
                             "total": -np.log(po if over else 1 - po),
                             "total_base": -np.log(bo if over else 1 - bo),
                             "winner": -np.log(pw if a > b else 1 - pw),
                             "winner_base": np.log(2), "p_over": po, "over": over,
                             "ot": a + b > 24, "p_ot": model.prob_overtime(dist)})
        ratings.update(t1, t2, tier, list(zip(g.t1_rounds, g.t2_rounds)))
    return pd.DataFrame(rows)


def report(res: pd.DataFrame, label: str = ""):
    print(f"{label}{len(res)} maps")
    for k in ("exact", "total", "winner"):
        d = res[k] - res[k + "_base"]
        print(f"  {k:7s} model {res[k].mean():.4f}  baseline {res[k + '_base'].mean():.4f}"
              f"  diff {d.mean():+.4f} (t={d.mean() / d.std() * np.sqrt(len(d)):+.1f})")
    print(f"  overtime: predicted {res.p_ot.mean():.3f}, actual {res.ot.mean():.3f}")
    print(f"  over 21.5: predicted {res.p_over.mean():.3f}, actual {res.over.mean():.3f}")
    res = res.assign(bucket=pd.cut(res.p_over, [0, .35, .45, .55, .65, 1]))
    print(res.groupby("bucket", observed=True).agg(n=("over", "size"), pred=("p_over", "mean"),
                                                    actual=("over", "mean")).round(3).to_string())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2025-01-01")
    ap.add_argument("--k", type=float)
    ap.add_argument("--kappa", type=float)
    ap.add_argument("--by-tier", action="store_true")
    args = ap.parse_args()
    load_dotenv()
    params = model.Params()
    if args.k:
        params.k = args.k
    if args.kappa:
        params.kappa = args.kappa
    df = load()
    res = run(df, params, args.start)
    report(res, f"k={params.k} kappa={params.kappa}: ")
    if args.by_tier:
        for tier, g in res.groupby("tier", dropna=False):
            report(g, f"tier {tier}: ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
