#!/usr/bin/env python
"""Telegram messages for the CS2 tips - the twin of the tennis notify.py: same
bot, same chat, with a 🎮 so the three sports are told apart.

    python notify.py             announce new tips and changed ones
    python notify.py --dry-run   print what would be sent, send nothing
    python notify.py --test      one message, to check the wiring

A tip is rewritten on every run until its match starts, so an announcement is
a snapshot: a tip can appear, move to another line or market on the same map,
change stake, or be withdrawn. cs2.advice remembers what Telegram was last
told (told_bet, told_rating, told_at), and the second kind of message reports
the difference. Nothing is marked as told until Telegram has accepted the
message, so a failed send is retried on the next run.
"""

import argparse
import html
import logging
import os
import sys
from zoneinfo import ZoneInfo

import psycopg
import requests
from dotenv import load_dotenv
from psycopg.rows import dict_row

log = logging.getLogger("notify")

PRAGUE = ZoneInfo("Europe/Prague")
TELEGRAM_LIMIT = 3500
SITE = "https://betken.cz/esport"
RATING_STEP = 2            # a stake move worth its own message
BOOKS = {"betano": "Betano", "chance": "Chance"}
MARKETS = {"rounds": "total rounds", "handicap": "round handicap", "team_rounds": "team rounds"}

# What a tip is, for "has it changed": the bet itself, not its price.
BET = "(market || '|' || team || '|' || side || '|' || line::float)"


def api(method, token, **params):
    """Telegram call. The token never reaches the log, only the method name."""
    r = requests.post(f"https://api.telegram.org/bot{token}/{method}", data=params, timeout=30)
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if not body.get("ok"):
        raise RuntimeError(f"Telegram {method} failed: {body.get('description') or r.status_code}")
    return body["result"]


def bet_text(market, team, side, line, t1, t2):
    who = {1: t1, 2: t2}.get(team)
    if market == "handicap":
        return f"{who} {line:+.1f}"
    return f"{who + ' ' if who else ''}{side.upper()} {line:.1f}"


def describe(r, changed=False):
    e = html.escape
    start = r["kickoff"].astimezone(PRAGUE)
    now = bet_text(r["market"], r["team"], r["side"], float(r["line"]), r["team1"], r["team2"])

    def tip(text, rating, odds=None):
        if not text:
            return "no bet"
        out = f"<b>{e(text)}</b>" + (f" @ {odds:.2f}" if odds else "")
        return out + (f" · stake {rating}/10" if rating else "")

    out = [f"<b>{e(r['team1'])} v {e(r['team2'])}</b>",
           f"{e(r['league'] or '')} · {start:%a %d %b %H:%M} · map {r['map_num']}, "
           f"{MARKETS.get(r['market'], r['market'])}"]
    if changed:
        was = None
        if r["told_bet"]:
            m, t, s, ln = r["told_bet"].split("|")
            was = bet_text(m, int(t), s, float(ln), r["team1"], r["team2"])
        out.append(f"{tip(was, r['told_rating'])} → "
                   f"{tip(now if r['backed'] else None, r['rating'], float(r['odds']))}")
    else:
        out.append(f"{tip(now, r['rating'], float(r['odds']))} · edge {float(r['edge']):+.1%} · "
                   f"{BOOKS.get(r['bookmaker'], r['bookmaker'])}")
    return "\n".join(out)


def fetch(conn, where):
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(f"SELECT *, {BET} AS bet FROM cs2.advice WHERE kickoff > now() AND {where} "
                    "ORDER BY kickoff, team1, slot")
        return cur.fetchall()


def build(rows, changed=False):
    n = f"{len(rows)} tip{'' if len(rows) == 1 else 's'}"
    if changed:
        header = f"🔄🎮 <b>CS2 advice changed</b> - {n}"
        footer = ("\n\n<i>Tips are recomputed until the match starts; the record keeps the last "
                  f"version before it.</i>\n<a href=\"{SITE}\">CS2 tips</a>")
    else:
        header = f"🎮 <b>New CS2 tips</b> - {n}"
        footer = ("\n\n<i>CS2 round lines at +10% value or more, at most one per map. A new model, "
                  f"not yet proven against Betano - small stakes.</i>\n<a href=\"{SITE}\">CS2 tips</a>")
    messages, current = [], header
    for r in rows:
        block = "\n\n" + describe(r, changed)
        if len(current) + len(block) + len(footer) > TELEGRAM_LIMIT:
            messages.append(current + footer)
            current = header + " (cont.)"
        current += block
    messages.append(current + footer)
    return messages


def mark_told(conn, rows):
    with conn.cursor() as cur:
        for r in rows:
            cur.execute(f"""UPDATE cs2.advice
                               SET told_bet = CASE WHEN backed THEN {BET} END,
                                   told_rating = rating, told_at = now()
                             WHERE (bookmaker, event_id, slot) = (%s, %s, %s)""",
                        (r["bookmaker"], r["event_id"], r["slot"]))
    conn.commit()


def main():
    ap = argparse.ArgumentParser(description="Telegram alerts for CS2 tips.")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--test", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    load_dotenv()
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")

    if args.test:
        if not (token and chat):
            raise SystemExit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID first.")
        api("sendMessage", token, chat_id=chat, text="🎮 CS2 tips are connected.")
        print("Test message sent.")
        return 0

    from cs2 import db
    conn = db.connect()
    fresh = fetch(conn, "told_at IS NULL AND backed")
    moved = fetch(conn, f"""told_at IS NOT NULL
        AND ((CASE WHEN backed THEN {BET} END) IS DISTINCT FROM told_bet
             OR (backed AND abs(COALESCE(rating, 0) - COALESCE(told_rating, 0)) >= {RATING_STEP}))""")
    if not (fresh or moved):
        log.info("No new CS2 tip, nothing changed.")
        return 0
    messages = (build(fresh) if fresh else []) + (build(moved, changed=True) if moved else [])
    if args.dry_run or not (token and chat):
        if not args.dry_run:
            log.info("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set - %d new and %d changed "
                     "tip(s) left unannounced.", len(fresh), len(moved))
        for m in messages:
            print(m, end="\n\n")
        return 0
    for m in messages:
        api("sendMessage", token, chat_id=chat, text=m, parse_mode="HTML",
            disable_web_page_preview="true")
    mark_told(conn, fresh + moved)
    log.info("Announced %d new and %d changed tip(s) in %d message(s).",
             len(fresh), len(moved), len(messages))
    return 0


if __name__ == "__main__":
    sys.exit(main())
