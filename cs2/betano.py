"""Betano's own JSON (www.betano.cz/api/...) for Counter-Strike matches.

    /api/sport/esports/                    every e-sport and its leagues
    /api/sport/esports/counter-strike/...  a league's matches
    /api/zapas-sance/{slug}/{id}/?bt=N     one match; N picks the market tab

Lines are per map *number* ("Mapa 1"), not per map name: the veto happens
just before play, so the map is unknown when the line is priced. Market
types are stable codes:

    TNXR     total rounds on map N, over/under a line (several lines)
    MXRH     round handicap on map N, per team
    1000543  one team's rounds on map N, over/under
    1000534  exact map score on map N ("13 - 9", "12 - 12" = overtime)
    5263     overtime on map N, yes/no
    TMPW     map N winner
    3642     map N result in regulation (team 1 / draw / team 2)

Read gently: a request a second, a few per match. A challenge page instead
of JSON stops the run; it never tries to get past one.
"""

import logging
import re
import time
from datetime import datetime, timezone

import requests

log = logging.getLogger(__name__)

BASE = "https://www.betano.cz"
GAP = 1.0
MARKETS = {"TNXR": "rounds", "MXRH": "handicap", "1000543": "team_rounds",
           "1000534": "map_score", "5263": "overtime", "TMPW": "map_winner",
           "3642": "map_result"}

_s = requests.Session()
_s.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                 "AppleWebKit/537.36 Chrome/128 Safari/537.36",
                   "Accept": "application/json"})
_last = 0.0


class Blocked(RuntimeError):
    """Betano answered with something other than its JSON."""


def _get(path: str) -> dict:
    global _last
    wait = GAP - (time.monotonic() - _last)
    if wait > 0:
        time.sleep(wait)
    _last = time.monotonic()
    r = _s.get(BASE + path, timeout=30)
    if r.status_code in (403, 429, 503) or "json" not in r.headers.get("content-type", ""):
        raise Blocked(f"{path}: HTTP {r.status_code}, {r.headers.get('content-type')}")
    r.raise_for_status()
    return r.json()["data"]


def leagues() -> list[dict]:
    out = []
    for group in _get("/api/sport/esports/").get("regionGroups", []):
        for region in group.get("regions", []):
            if region.get("name") != "Counter Strike":
                continue
            for lg in region.get("leagues", []):
                out.append({"name": lg.get("name", ""), "url": lg.get("url", "")})
    return out


def league_events(league: dict) -> list[dict]:
    """Matches still to start."""
    data = _get("/api" + league["url"])
    now = datetime.now(timezone.utc)
    out = []
    for block in data.get("blocks", []):
        for e in block.get("events", []):
            parts = e.get("participants") or []
            start = datetime.fromtimestamp(e["startTime"] / 1000, timezone.utc)
            if e.get("liveNow") or len(parts) != 2 or start <= now:
                continue
            out.append({"event_id": str(e["id"]), "kickoff": start, "league": league["name"],
                        "url": e["url"], "team1": parts[0]["name"], "team2": parts[1]["name"],
                        "team1_id": str(parts[0].get("id")), "team2_id": str(parts[1].get("id"))})
    return out


_MAP = re.compile(r"\(Mapa (\d+)")


def _which_team(prefix: str, event: dict) -> int | None:
    """Which team a market title names. Betano may shorten it ('Legacy' for
    'Legacy Esports'), so a name that starts the other counts; two hits or
    none is no answer."""
    a = prefix.strip().lower()
    hits = [i for i, t in ((1, event["team1"]), (2, event["team2"]))
            if a == t.lower() or t.lower().startswith(a) or a.startswith(t.lower())]
    return hits[0] if len(hits) == 1 else None


def event_odds(event: dict) -> list[dict]:
    """Every selection of the round markets, one row each."""
    path = "/api" + event["url"]
    data = _get(path)
    tabs = data.get("markets") or data.get("nonBetBuilderTabs") or []
    alls = [t["id"] for t in tabs if t.get("type") in ("all", "allmarkets")]
    ev = _get(f"{path}?bt={alls[0]}")["event"] if alls else data.get("event", {})
    rows = []
    for m in ev.get("markets", []):
        kind = MARKETS.get(str(m.get("type")))
        mm = _MAP.search(m.get("name", ""))
        if not kind or not mm:
            continue
        team = None
        if kind == "team_rounds":
            team = _which_team(m["name"].split(" Celkový")[0], event)
            if team is None:
                log.warning("%s: no team for market %r", event["url"], m["name"])
                continue
        for sel in m.get("selections", []):
            name = sel.get("name", "")
            side, line = None, sel.get("handicap")
            if kind in ("rounds", "team_rounds"):
                side = "over" if name.startswith("Více") else "under" if name.startswith("Méně") else None
            elif kind == "handicap":
                tid = str(sel.get("teamId"))
                team = 1 if tid == event["team1_id"] else 2 if tid == event["team2_id"] else None
            elif kind == "overtime":
                side = "yes" if name == "Ano" else "no" if name == "Ne" else None
            elif kind in ("map_winner", "map_result"):
                team = 1 if name == event["team1"] else 2 if name == event["team2"] else 0
                line = None
            elif kind == "map_score":
                side, line = name.replace(" ", ""), None
            rows.append({"market": kind, "map_num": int(mm.group(1)), "team": team,
                         "side": side, "line": line, "price": sel.get("price")})
    return rows
