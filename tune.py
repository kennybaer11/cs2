"""Grid over the learning rate k and the spread kappa: fit on 2025, report 2026."""
import itertools, sys, warnings
import pandas as pd
from dotenv import load_dotenv
import backtest
from cs2 import model
warnings.filterwarnings("ignore")
load_dotenv()
df = backtest.load()
out = []
for k, kappa in itertools.product((0.003, 0.006, 0.009), (22, 30)):
    p = model.Params(k=k, kappa=kappa)
    r = backtest.run(df, p, "2025-01-01")
    r["year"] = r.played_at.dt.year
    for y, g in r.groupby("year"):
        out.append({"k": k, "kappa": kappa, "year": y, "exact": g.exact.mean(), "total": g.total.mean(),
                    "winner": g.winner.mean(), "p_over": g.p_over.mean(), "over": g.over.mean()})
    print(k, kappa, flush=True, file=sys.stderr)
res = pd.DataFrame(out)
print(res.pivot_table(index=["k", "kappa"], columns="year", values=["exact", "total", "winner"]).round(4).to_string())
print(res[res.year == 2026][["k", "kappa", "p_over", "over"]].round(3).to_string(index=False))
