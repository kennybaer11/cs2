# cs2 — pricing Counter-Strike 2 round lines

The e-sports counterpart to posession (football possession) and aces (tennis
aces): estimate the score distribution of every map, turn it into a
probability for any round line, and compare that with the bookmakers' prices.

Separate from posession, but shares its Neon database through its own `cs2`
schema, so `DATABASE_URL` is the same string.

## Markets

Betano (`cs2/betano.py`) and Chance.cz price, per map **number** (the veto
comes just before play, so the map itself is unknown):

- total rounds, over/under a ladder of lines
- round handicap per team
- one team's rounds, over/under
- overtime yes/no, exact map score

All of them follow from one distribution of the map's final score.

## Data

Liquipedia's Counter-Strike wiki through its MediaWiki API
(`cs2/liquipedia.py`): every S-, A- and B-tier CS2 event from 2024, each map
with both teams' rounds per half and per overtime. HLTV refuses scripts (403).

Liquipedia's API terms: an identifying User-Agent, gzip, one request every
2 seconds, one `action=parse` every 30 seconds. Its content is **CC-BY-SA 3.0**:
anything published from it credits Liquipedia.

```bash
python collect.py           # about an hour the first time; later only events still on
python teams.py             # resolve team template keys to team pages
python odds.py              # store Betano's round lines for matches still to start
```

## The model

`cs2/model.py`:

1. **Ratings.** Team 1's expected share of rounds is logistic(R1 - R2). After
   each match the ratings move along the gradient of a binomial likelihood on
   the rounds won (online logistic regression). New teams start at their
   tournament tier's prior.
2. **One map.** The chance of winning a round on the day is spread around that
   share as a Beta distribution (map, form, momentum); given it, the CS2 rules
   (first to 13, overtime blocks of six, first to 4) give every final score
   exactly.

```bash
python backtest.py --start 2025-01-01      # walk-forward, against a no-information baseline
python tune.py                             # grid over k and kappa: fit on 2025, check on 2026
```

Tuned 7 Nov 2026: k = 0.006, kappa = 22. On 2026 maps the score log-loss is
2.977 against about 3.05 for the baseline, and the map winner 0.656 against 0.693.

## Prices and settlement

```bash
python price.py --store     # the model's probability and edge on every stored line -> cs2.priced
python settle.py            # pair played matches with Liquipedia results, settle the lines
```

A line keeps its last price before kick-off. A line on a map that was not
played is void. betken.cz's E-sport pages read cs2.priced: the public page says
no tips are given yet; the admin page shows every line and the scoreboard -
the model's log-loss against Betano's own probability with its margin taken
out, and flat-stake results on lines the model rated +10% or better.

## Tips

```bash
python advice.py            # after price.py --store: cs2.advice, the tips on betken.cz/esport
```

The same rules as the tennis tips: the model's chance blended 3:1 with
Betano's own (margin out), +10% value or more, odds 1.40-4.50, a price on both
sides of the line, at most one tip per map, quarter-Kelly stakes shared per
match. A tip is rewritten until its match starts and locked by the database
from then on; settle.py settles the tips with the lines.

## On the server

`deploy/run_cs2.sh` in the posession repo runs every three hours on the
posession server: collect, teams, settle, price, advice, notify.

**Betano refuses the server** (HTTP 403 on every request, tennis too, since at
least 6 Nov 2026), so its lines are fetched from the owner's PC, like the
tennis Betano lines. This fetches the lines, prices them, writes the tips and
tells Telegram:

```powershell
powershell -ExecutionPolicy Bypass -File C:\Users\kenny\cs2\betano_local.ps1
```
