"""Resolve Liquipedia team template keys to team pages (cs2.team).

    python teams.py          # only keys not resolved yet

One expandtemplates call renders a few hundred {{Team|key}} at once; each
renders as a link to the team's page, which is the team's identity.
"""

import logging
import re
import sys

from dotenv import load_dotenv

from cs2 import db, liquipedia as lp

log = logging.getLogger("teams")
CHUNK = 150


def resolve(keys: list[str]) -> dict[str, tuple[str | None, str | None]]:
    out = {}
    for i in range(0, len(keys), CHUNK):
        chunk = keys[i:i + CHUNK]
        text = "\n".join(f"@@{k}@@{{{{Team|{k}}}}}" for k in chunk)
        html = lp._call({"action": "parse", "text": text, "prop": "text",
                         "contentmodel": "wikitext", "disablelimitreport": 1},
                        cache_key="teams|" + "|".join(chunk))["parse"]["text"]
        for k, part in re.findall(r"@@(.+?)@@(.*?)(?=@@|$)", html, re.S):
            link = re.search(r'<a href="/counterstrike/([^"#?]+)"[^>]*title="([^"]+)"', part)
            if link and "redlink" not in link.group(0):
                out[k] = (lp.requests.utils.unquote(link.group(1)).replace("_", " "), link.group(2))
            else:
                name = re.sub(r"<[^>]+>", "", part).strip()
                out[k] = (None, name or None)
    return out


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    load_dotenv()
    conn = db.connect()
    with conn.cursor() as cur:
        cur.execute("""SELECT k FROM (SELECT team1 k FROM cs2.match UNION SELECT team2 FROM cs2.match) x
                        WHERE k NOT IN (SELECT key FROM cs2.team) ORDER BY k""")
        keys = [r[0] for r in cur.fetchall()]
    conn.close()                      # the API calls take minutes; Neon drops idle links
    log.info("%d keys to resolve", len(keys))
    found = resolve(keys)
    conn = db.connect()
    with conn.cursor() as cur:
        cur.executemany("INSERT INTO cs2.team (key, page, name) VALUES (%s,%s,%s) "
                        "ON CONFLICT (key) DO UPDATE SET page=EXCLUDED.page, name=EXCLUDED.name",
                        [(k, p or f"?{k}", n) for k, (p, n) in found.items()])
    conn.commit()
    pages = {p for p, _ in found.values() if p}
    log.info("%d keys -> %d team pages, %d without a page", len(found), len(pages),
             sum(1 for p, _ in found.values() if not p))
    return 0


if __name__ == "__main__":
    sys.exit(main())
