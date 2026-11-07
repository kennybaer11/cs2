"""The rounds model: a team rating, and the score distribution of one map.

Ratings. Each team has a number R. Team 1's expected share of rounds against
team 2 is m = logistic(R1 - R2). After a match, every map's rounds move the
ratings along the gradient of a binomial likelihood: R1 += K * (won - m*played)
/ sqrt-ish scale, which is online logistic regression. A new team starts at the
prior of its tournament's tier, because B-tier newcomers are not S-tier ones.
A match's maps are all predicted before any of them updates the ratings: the
bookmaker prices the whole match before map 1 too.

One map. The chance team 1 wins a round on the day, p, is spread around m as
Beta(m*kappa, (1-m)*kappa) - which map, which form, momentum. Given p, rounds
are independent and the CS2 rules give each final score exactly:

    13-k (k = 0..11)   C(12+k, k) p^13 q^k
    12-12 -> overtime  blocks of six rounds, first to 4; at 3-3 another block

The mixture over p is a fixed quadrature on Beta quantiles.
"""

from dataclasses import dataclass, field
from functools import lru_cache
from math import comb

import numpy as np
from scipy import stats

MAX_OT = 6                    # overtime blocks; the rest of the tail is negligible
N_Q = 48                      # quadrature points for the Beta mixture


def scores() -> list[tuple[int, int]]:
    """Every final score the distribution covers, team 1 first."""
    out = [(13, k) for k in range(12)] + [(k, 13) for k in range(12)]
    for n in range(MAX_OT):
        base = 12 + 3 * n
        out += [(base + 4, base + j) for j in range(3)] + [(base + j, base + 4) for j in range(3)]
    return out


SCORES = scores()
INDEX = {s: i for i, s in enumerate(SCORES)}
T1 = np.array([s[0] for s in SCORES])
T2 = np.array([s[1] for s in SCORES])
TOTAL = T1 + T2


def _given_p(p: np.ndarray) -> np.ndarray:
    """P(score) for each p: shape (len(p), len(SCORES))."""
    q = 1 - p
    cols = [comb(12 + k, k) * p ** 13 * q ** k for k in range(12)]
    cols += [comb(12 + k, k) * q ** 13 * p ** k for k in range(12)]
    tie = comb(24, 12) * p ** 12 * q ** 12
    ot_tie = comb(6, 3) * p ** 3 * q ** 3
    for n in range(MAX_OT):
        reach = tie * ot_tie ** n
        cols += [reach * comb(3 + j, j) * p ** 4 * q ** j for j in range(3)]
        cols += [reach * comb(3 + j, j) * q ** 4 * p ** j for j in range(3)]
    out = np.stack(cols, axis=1)
    return out / out.sum(axis=1, keepdims=True)


@lru_cache(maxsize=4096)
def _dist_cached(m: float, kappa: float) -> np.ndarray:
    u = (np.arange(N_Q) + 0.5) / N_Q
    p = stats.beta.ppf(u, m * kappa, (1 - m) * kappa)
    p = np.clip(p, 1e-4, 1 - 1e-4)
    return _given_p(p).mean(axis=0)


def score_dist(m: float, kappa: float) -> np.ndarray:
    """Probability of each score in SCORES when team 1's expected round share is m."""
    return _dist_cached(round(float(m), 3), round(float(kappa), 1))


# ---------------------------------------------------------------- the ratings

@dataclass
class Params:
    k: float = 0.006              # learning rate per round (tune.py: best on 2025, held on 2026)
    kappa: float = 22.0           # Beta concentration: larger = less spread between maps
    prior: dict = field(default_factory=lambda: {1: 0.25, 2: 0.1, 3: 0.0, None: -0.1})


class Ratings:
    def __init__(self, params: Params):
        self.p = params
        self.r: dict[str, float] = {}
        self.n: dict[str, int] = {}

    def get(self, team: str, tier) -> float:
        if team not in self.r:
            self.r[team] = self.p.prior.get(tier, self.p.prior[None])
            self.n[team] = 0
        return self.r[team]

    def expected(self, t1: str, t2: str, tier) -> float:
        d = self.get(t1, tier) - self.get(t2, tier)
        return 1 / (1 + np.exp(-d))

    def update(self, t1: str, t2: str, tier, maps: list[tuple[int, int]]):
        m = self.expected(t1, t2, tier)
        g = sum(a - m * (a + b) for a, b in maps)
        # A team's first matches move it faster: less is known about it.
        for team, sign in ((t1, 1), (t2, -1)):
            boost = 1 + 2 / (1 + self.n[team] / 5)
            self.r[team] += sign * self.p.k * boost * g
            self.n[team] += len(maps)


# ------------------------------------------------------------- market prices

def prob_total_over(dist: np.ndarray, line: float) -> float:
    return float(dist[TOTAL > line].sum())


def prob_handicap(dist: np.ndarray, team: int, line: float) -> float:
    """Team covers 'team line' (e.g. team 1 -3.5): its rounds + line > the other's."""
    diff = (T1 - T2) if team == 1 else (T2 - T1)
    return float(dist[diff + line > 0].sum())


def prob_team_over(dist: np.ndarray, team: int, line: float) -> float:
    r = T1 if team == 1 else T2
    return float(dist[r > line].sum())


def prob_overtime(dist: np.ndarray) -> float:
    return float(dist[TOTAL > 24].sum())


def prob_exact(dist: np.ndarray, score: str) -> float:
    """'13-9', or '12-12' meaning the map went to overtime."""
    a, b = (int(x) for x in score.split("-"))
    if (a, b) == (12, 12):
        return prob_overtime(dist)
    i = INDEX.get((a, b))
    return float(dist[i]) if i is not None else 0.0
