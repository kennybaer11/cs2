"""Liquipedia's Counter-Strike wiki, through its MediaWiki API.

Liquipedia's API terms (liquipedia.net/api-terms-of-use): an identifying
User-Agent, gzip, at most one request every 2 seconds, and at most one
action=parse every 30 seconds. Content is CC-BY-SA 3.0, so anything published
from it credits Liquipedia.

Only two kinds of call are used:

    action=parse        the tier lists, whose rows are generated (2 calls a run)
    action=query        page wikitext, up to 50 pages a call, and subpage lists

Match results come from the wikitext of each tournament page and its
subpages: every match is a {{Match ...}} template with one {{Map ...}} per
map, carrying each team's rounds per half and per overtime.
"""

import hashlib
import json
import logging
import re
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

log = logging.getLogger(__name__)

API = "https://liquipedia.net/counterstrike/api.php"
CACHE = Path(__file__).resolve().parent.parent / "cache"
GAP = {"query": 2.1, "parse": 31.0}
_s = requests.Session()
_s.headers.update({"User-Agent": "betken-cs2-model/0.1 (CS2 round statistics; https://betken.cz)",
                   "Accept-Encoding": "gzip"})
_last = {"query": 0.0, "parse": 0.0}


def _call(params: dict, cache_key: str | None = None) -> dict:
    """One API call, paced by its kind. With cache_key the answer is kept on
    disk for good - only for pages that no longer change."""
    if cache_key:
        h = hashlib.sha1(cache_key.encode()).hexdigest()
        p = CACHE / h[:2] / f"{h}.json"
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    kind = params["action"]
    for attempt in range(5):
        now = time.monotonic()
        wait = max(GAP["query"] - (now - max(_last.values())),     # any call: 2 s apart
                   GAP[kind] - (now - _last[kind]))                 # parse: 30 s apart
        if wait > 0:
            time.sleep(wait)
        _last[kind] = time.monotonic()
        try:
            full = {**params, "format": "json", "formatversion": 2}
            # Rendering a batch of templates sends long text: POST, or the URL is too long.
            r = (_s.post(API, data=full, timeout=60) if "text" in params
                 else _s.get(API, params=full, timeout=60))
        except requests.RequestException as e:
            log.warning("%s (attempt %d)", e, attempt + 1)
            time.sleep(10 * (attempt + 1))
            continue
        if r.status_code in (429, 500, 502, 503, 504):
            log.warning("HTTP %d (attempt %d), backing off", r.status_code, attempt + 1)
            time.sleep(60 * (attempt + 1))
            continue
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            raise RuntimeError(f"Liquipedia: {data['error']}")
        if cache_key:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(data), encoding="utf-8")
        return data
    raise RuntimeError(f"gave up on {params}")


# ---------------------------------------------------------------- tier lists

TIER_PAGES = {1: ["S-Tier_Tournaments/Post_2023"], 2: ["A-Tier_Tournaments/Post_2023"],
              3: ["B-Tier_Tournaments/2024", "B-Tier_Tournaments/2025", "B-Tier_Tournaments/2026"],
              # C-tier from mid-2025 (ESL Challenger League cups and the like):
              # enough history to rate its teams, and Betano prices its matches.
              4: ["C-Tier_Tournaments/2025_Q3", "C-Tier_Tournaments/2025_Q4"]
                 + [f"C-Tier_Tournaments/{y}_Q{q}" for y in range(2026, date.today().year + 1)
                    for q in range(1, 5)]}


def _list_is_past(page: str) -> bool:
    """A year or quarter list that ended before today's quarter never changes,
    so its rendering can be cached - each one is a 30-second parse call."""
    m = re.search(r"/(\d{4})(?:_Q(\d))?$", page)
    if not m:
        return False
    year, q = int(m[1]), int(m[2] or 4)
    today = date.today()
    return (year, q) < (today.year, (today.month - 1) // 3 + 1) and year < today.year + 1 \
        and not (m[2] is None and year == today.year)


def tier_list(tier: int) -> list[str]:
    """Titles of the tournaments on a tier's list pages, as linked."""
    html = "".join(_call({"action": "parse", "page": page, "prop": "text"},
                         cache_key=("list|" + page) if _list_is_past(page) else None)["parse"]["text"]
                   for page in TIER_PAGES[tier])
    titles = re.findall(r'class="column&#95;&#95;tournament"[^>]*>\s*<a href="/counterstrike/([^"]+)"',
                        html)
    out = []
    for t in titles:
        t = requests.utils.unquote(t).replace("_", " ")
        if t not in out:
            out.append(t)
    return out


# ------------------------------------------------------------------- wikitext

def subpages(title: str) -> list[str]:
    data = _call({"action": "query", "list": "allpages", "apprefix": title + "/",
                  "aplimit": 500})
    return [p["title"] for p in data["query"]["allpages"]]


def wikitext(titles: list[str], cache: bool = False) -> dict[str, str]:
    """{title: wikitext}, following redirects (the key is the title asked for)."""
    out = {}
    for i in range(0, len(titles), 50):
        chunk = titles[i:i + 50]
        key = "wt|" + "|".join(chunk) if cache else None
        data = _call({"action": "query", "prop": "revisions", "rvprop": "content",
                      "rvslots": "main", "titles": "|".join(chunk), "redirects": 1}, key)
        q = data["query"]
        back = {r["to"]: r["from"] for r in q.get("redirects", [])}
        back.update({n["to"]: n["from"] for n in q.get("normalized", [])})
        for p in q.get("pages", []):
            if p.get("missing") or not p.get("revisions"):
                continue
            t = p["title"]
            while t in back and back[t] != t:
                t = back[t]
            out[t] = p["revisions"][0]["slots"]["main"]["content"]
    return out


def _templates(text: str, name: str) -> list[str]:
    """The bodies of every {{name ...}} in text, braces balanced."""
    out = []
    pat = re.compile(r"\{\{\s*" + re.escape(name) + r"\s*[|}\n]", re.I)
    for m in pat.finditer(text):
        i, depth = m.start(), 0
        j = i
        while j < len(text) - 1:
            two = text[j:j + 2]
            if two == "{{":
                depth += 1
                j += 2
            elif two == "}}":
                depth -= 1
                j += 2
                if depth == 0:
                    out.append(text[i + 2:j - 2])
                    break
            else:
                j += 1
    return out


def _params(body: str) -> dict[str, str]:
    """Top-level |key=value pairs of one template body; positional ones as 1, 2, ..."""
    parts, depth, cur = [], 0, []
    i = 0
    while i < len(body):
        two = body[i:i + 2]
        if two in ("{{", "[["):
            depth += 1
            cur.append(two)
            i += 2
            continue
        if two in ("}}", "]]"):
            depth -= 1
            cur.append(two)
            i += 2
            continue
        if body[i] == "|" and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(body[i])
        i += 1
    parts.append("".join(cur))
    out, pos = {}, 0
    for p in parts[1:]:
        if "=" in p and not p.lstrip().startswith("{{"):
            k, v = p.split("=", 1)
            out[k.strip()] = v.strip()
        else:
            pos += 1
            out[str(pos)] = p.strip()
    return out


def infobox(text: str) -> dict[str, str]:
    boxes = _templates(text, "Infobox league")
    return _params(boxes[0]) if boxes else {}


TZ = {"UTC": 0, "GMT": 0, "WET": 0, "BST": 1, "CET": 1, "CEST": 2, "EET": 2, "EEST": 3,
      "MSK": 3, "AST": 3, "GST": 4, "PKT": 5, "IST": 5.5, "ICT": 7, "WIB": 7, "CST": 8,
      "SGT": 8, "PHT": 8, "HKT": 8, "AWST": 8, "KST": 9, "JST": 9, "AEST": 10, "AEDT": 11,
      "NZDT": 13, "BRT": -3, "ART": -3, "CLT": -4, "EDT": -4, "EST": -5, "CDT": -5,
      "MDT": -6, "MST": -7, "PDT": -7, "PST": -8}


def parse_date(s: str) -> datetime | None:
    """'November 2, 2026 - 10:00 {{Abbr/CST}}' -> UTC. A missing zone is UTC."""
    if not s:
        return None
    zone = re.search(r"Abbr/(\w+)", s)
    off = TZ.get(zone.group(1), 0) if zone else 0
    s = re.sub(r"\{\{.*?\}\}", "", s).strip()
    for fmt in ("%B %d, %Y - %H:%M", "%B %d, %Y", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            d = datetime.strptime(s, fmt)
            return (d - timedelta(hours=off)).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return None


def parse_day(s: str) -> date | None:
    s = (s or "").strip()
    for fmt in ("%Y-%m-%d",):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    m = re.match(r"(\d{4})-(\d{2})-\?\?", s)
    return date(int(m[1]), int(m[2]), 1) if m else None


def _team(v: str) -> str | None:
    """{{TeamOpponent|legacy}} or {{TeamOpponent|template=legacy}} -> 'legacy'."""
    if not v:
        return None
    t = _templates(v, "TeamOpponent")
    if not t:
        return None
    p = _params(t[0])
    team = (p.get("template") or p.get("1") or "").strip().lower()
    return None if team in ("", "tbd", "tba", "bye") else team


def _int(v) -> int:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return 0


def _map(body: str) -> dict | None:
    p = _params(body)
    if p.get("finished", "").lower() not in ("true", "1", "yes") and not p.get("winner"):
        return None
    name = p.get("map", "").strip()
    if not name or name.upper() in ("TBA", "TBD", "DEFAULT"):
        return None
    if p.get("finished", "").lower() == "skip" or p.get("walkover"):
        return None
    reg1 = _int(p.get("t1t")) + _int(p.get("t1ct"))
    reg2 = _int(p.get("t2t")) + _int(p.get("t2ct"))
    ot1 = sum(_int(v) for k, v in p.items() if re.fullmatch(r"o\d+t1(t|ct)", k))
    ot2 = sum(_int(v) for k, v in p.items() if re.fullmatch(r"o\d+t2(t|ct)", k))
    if reg1 + reg2 == 0:                    # some pages give only the score
        reg1, reg2 = _int(p.get("score1")), _int(p.get("score2"))
    if reg1 + reg2 == 0:
        return None
    first = p.get("t1firstside", "").lower()
    return {"map_name": name, "t1_rounds": reg1 + ot1, "t2_rounds": reg2 + ot2,
            "t1_ct": _int(p.get("t1ct")), "t1_t": _int(p.get("t1t")),
            "t2_ct": _int(p.get("t2ct")), "t2_t": _int(p.get("t2t")),
            "t1_first_side": first if first in ("ct", "t") else None,
            "ot_rounds": ot1 + ot2}


def matches(page: str, text: str) -> list[dict]:
    """Every finished match on one page, with its played maps in order."""
    out = []
    for n, body in enumerate(_templates(text, "Match")):
        p = _params(body)
        t1, t2 = _team(p.get("opponent1", "")), _team(p.get("opponent2", ""))
        if not t1 or not t2:
            continue
        maps = []
        for k in sorted((k for k in p if re.fullmatch(r"map\d+", k)), key=lambda k: int(k[3:])):
            inner = _templates(p[k], "Map")
            m = _map(inner[0]) if inner else None
            if m:
                m["map_num"] = int(k[3:])
                maps.append(m)
        if not maps:
            continue
        played = parse_date(p.get("date", ""))
        hltv = p.get("hltv", "").strip() or None
        key = hltv or f"{page}|{n}|{t1}|{t2}"
        mid = "h" + hltv if hltv else "l" + hashlib.sha1(key.encode()).hexdigest()[:16]
        out.append({"match_id": mid, "page": page, "played_at": played, "team1": t1,
                    "team2": t2, "hltv_id": hltv, "maps": maps,
                    "bestof": _int(p.get("bestof")) or None})
    return out
