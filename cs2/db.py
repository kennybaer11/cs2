"""Postgres writes. Every table is in the `cs2` schema."""

import os
from pathlib import Path

import psycopg

SCHEMA = Path(__file__).resolve().parent.parent / "schema.sql"


def connect():
    conn = psycopg.connect(os.environ["DATABASE_URL"], autocommit=False)
    with conn.cursor() as cur:
        cur.execute(SCHEMA.read_text(encoding="utf-8"))
    conn.commit()
    return conn


def finished_tournaments(conn) -> set[str]:
    """Tournaments over for more than a week, with matches stored: never re-read."""
    with conn.cursor() as cur:
        cur.execute("""SELECT t.page FROM cs2.tournament t
                        WHERE t.end_date < current_date - 7
                          AND EXISTS (SELECT 1 FROM cs2.match m WHERE m.tournament = t.page)""")
        return {r[0] for r in cur.fetchall()}


TOUR_COLS = ["page", "name", "tier", "game", "start_date", "end_date", "lan"]
MATCH_COLS = ["match_id", "tournament", "page", "played_at", "team1", "team2", "bestof", "hltv_id"]
MAP_COLS = ["match_id", "map_num", "map_name", "t1_rounds", "t2_rounds", "t1_ct", "t1_t",
            "t2_ct", "t2_t", "t1_first_side", "ot_rounds"]


def save_tournament(conn, t: dict, matches: list[dict]):
    _upsert(conn, "tournament", TOUR_COLS, [t], ["page"])
    _upsert(conn, "match", MATCH_COLS, [{**m, "tournament": t["page"]} for m in matches],
            ["match_id"])
    _upsert(conn, "map", MAP_COLS,
            [{**mp, "match_id": m["match_id"]} for m in matches for mp in m["maps"]],
            ["match_id", "map_num"])
    conn.commit()


def _upsert(conn, table: str, cols: list[str], rows: list[dict], key: list[str]):
    if not rows:
        return
    updates = ", ".join(f"{c}=EXCLUDED.{c}" for c in cols if c not in key)
    sql = (f"INSERT INTO cs2.{table} ({', '.join(cols)}) "
           f"VALUES ({', '.join(['%s'] * len(cols))}) "
           f"ON CONFLICT ({', '.join(key)}) DO UPDATE SET {updates}")
    with conn.cursor() as cur:
        cur.executemany(sql, [[r.get(c) for c in cols] for r in rows])


def save_odds(conn, bookmaker: str, e: dict, rows: list[dict]):
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO cs2.bk_event (bookmaker, event_id, kickoff, league, team1, team2, url)
                       VALUES (%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (bookmaker, event_id) DO UPDATE
                          SET kickoff=EXCLUDED.kickoff, league=EXCLUDED.league,
                              url=EXCLUDED.url, last_seen=now()""",
                    (bookmaker, e["event_id"], e["kickoff"], e["league"], e["team1"], e["team2"],
                     e.get("url")))
        cur.executemany(
            """INSERT INTO cs2.odds (bookmaker, event_id, market, map_num, team, side, line,
                                     price, price_open)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (bookmaker, event_id, market, map_num, team, side, line) DO UPDATE
                  SET price=EXCLUDED.price, last_seen=now()""",
            [(bookmaker, e["event_id"], r["market"], r["map_num"], r["team"] or 0,
              r["side"] or "", r["line"] or 0, r["price"], r["price"])
             for r in rows if r.get("price")])
    conn.commit()
