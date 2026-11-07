-- cs2: Counter-Strike 2 map results from Liquipedia, for pricing round lines
-- (total rounds per map, round handicaps, rounds per team).
--
-- Its own schema, so it shares the Neon database with posession, aces and
-- emptynet without colliding.

CREATE SCHEMA IF NOT EXISTS cs2;

CREATE TABLE IF NOT EXISTS cs2.tournament (
  page        TEXT PRIMARY KEY,           -- Liquipedia title, e.g. Intel Extreme Masters/2026/Beijing
  name        TEXT,
  tier        INTEGER,                    -- 1 S, 2 A, 3 B (Liquipedia's liquipediatier)
  game        TEXT,                       -- cs2
  start_date  DATE,
  end_date    DATE,
  lan         BOOLEAN,                    -- type=offline
  fetched_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One row per finished series. team1/team2 are Liquipedia's team template
-- names (lower case), in the page's own order - not winner and loser.
CREATE TABLE IF NOT EXISTS cs2.match (
  match_id    TEXT PRIMARY KEY,           -- 'h' + HLTV id when the page gives it
  tournament  TEXT NOT NULL REFERENCES cs2.tournament (page) ON DELETE CASCADE,
  page        TEXT NOT NULL,              -- the subpage the match is on
  played_at   TIMESTAMPTZ,
  team1       TEXT NOT NULL,
  team2       TEXT NOT NULL,
  bestof      INTEGER,
  hltv_id     TEXT
);
CREATE INDEX IF NOT EXISTS match_played_idx ON cs2.match (played_at);
CREATE INDEX IF NOT EXISTS match_team1_idx ON cs2.match (team1);
CREATE INDEX IF NOT EXISTS match_team2_idx ON cs2.match (team2);

-- One row per played map. Rounds include overtime; ot_rounds is how many of
-- them were overtime. The half columns are regulation only.
CREATE TABLE IF NOT EXISTS cs2.map (
  match_id      TEXT    NOT NULL REFERENCES cs2.match (match_id) ON DELETE CASCADE,
  map_num       INTEGER NOT NULL,
  map_name      TEXT    NOT NULL,
  t1_rounds     INTEGER NOT NULL,
  t2_rounds     INTEGER NOT NULL,
  t1_ct         INTEGER,
  t1_t          INTEGER,
  t2_ct         INTEGER,
  t2_t          INTEGER,
  t1_first_side TEXT,                   -- ct | t
  ot_rounds     INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (match_id, map_num)
);

-- Bookmaker matches, and every selection of their round markets. A line is
-- one row, rewritten while it is on offer: price is the latest, price_open the
-- first seen. Lines are per map NUMBER - the veto is not known when priced.
CREATE TABLE IF NOT EXISTS cs2.bk_event (
  bookmaker   TEXT NOT NULL,              -- betano | chance
  event_id    TEXT NOT NULL,
  kickoff     TIMESTAMPTZ NOT NULL,
  league      TEXT,
  team1       TEXT NOT NULL,              -- the bookmaker's names and order
  team2       TEXT NOT NULL,
  match_id    TEXT REFERENCES cs2.match (match_id) ON DELETE SET NULL,
  swapped     BOOLEAN,                    -- bookmaker team1 is Liquipedia's team2
  first_seen  TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen   TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (bookmaker, event_id)
);

CREATE TABLE IF NOT EXISTS cs2.odds (
  bookmaker   TEXT    NOT NULL,
  event_id    TEXT    NOT NULL,
  market      TEXT    NOT NULL,           -- rounds, handicap, team_rounds, map_score, overtime, map_winner, map_result
  map_num     INTEGER NOT NULL,
  team        INTEGER NOT NULL DEFAULT 0, -- 1/2 in the bookmaker's order, 0 none or draw
  side        TEXT    NOT NULL DEFAULT '',-- over/under, yes/no, exact score
  line        NUMERIC NOT NULL DEFAULT 0,
  price       NUMERIC NOT NULL,
  price_open  NUMERIC NOT NULL,
  first_seen  TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen   TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (bookmaker, event_id, market, map_num, team, side, line),
  FOREIGN KEY (bookmaker, event_id) REFERENCES cs2.bk_event (bookmaker, event_id) ON DELETE CASCADE
);

-- Liquipedia team template keys -> the team's page. Several keys can name one
-- team ('vit', 'vitality'); ratings are kept per page.
CREATE TABLE IF NOT EXISTS cs2.team (
  key         TEXT PRIMARY KEY,           -- as in cs2.match.team1/team2
  page        TEXT,                       -- Liquipedia team page, the identity
  name        TEXT                        -- the name the template shows
);

-- The model's price on each stored line, rewritten by price.py --store while
-- the match is still to start (so the last one before kickoff stays).
-- betken.cz's E-sport pages read this.
CREATE TABLE IF NOT EXISTS cs2.priced (
  bookmaker   TEXT    NOT NULL,
  event_id    TEXT    NOT NULL,
  market      TEXT    NOT NULL,
  map_num     INTEGER NOT NULL,
  team        INTEGER NOT NULL DEFAULT 0,
  side        TEXT    NOT NULL DEFAULT '',
  line        NUMERIC NOT NULL DEFAULT 0,
  price       NUMERIC NOT NULL,
  p_model     NUMERIC NOT NULL,
  edge        NUMERIC NOT NULL,            -- p_model * price - 1
  m           NUMERIC NOT NULL,            -- team 1's expected round share
  priced_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (bookmaker, event_id, market, map_num, team, side, line),
  FOREIGN KEY (bookmaker, event_id) REFERENCES cs2.bk_event (bookmaker, event_id) ON DELETE CASCADE
);
ALTER TABLE cs2.bk_event ADD COLUMN IF NOT EXISTS url TEXT;
ALTER TABLE cs2.bk_event ADD COLUMN IF NOT EXISTS team1_page TEXT;
ALTER TABLE cs2.bk_event ADD COLUMN IF NOT EXISTS team2_page TEXT;

-- Settlement (settle.py): each priced line's outcome once its match is in the
-- Liquipedia results. result: won | lost | void (map not played). The two
-- sides' prices at the close are both kept so the model can be scored against
-- the bookmaker's own no-vig probability, not only on bets.
ALTER TABLE cs2.priced ADD COLUMN IF NOT EXISTS result TEXT;
ALTER TABLE cs2.priced ADD COLUMN IF NOT EXISTS actual INTEGER;   -- the stat the line is on
ALTER TABLE cs2.priced ADD COLUMN IF NOT EXISTS settled_at TIMESTAMPTZ;
ALTER TABLE cs2.priced ADD COLUMN IF NOT EXISTS other_price NUMERIC; -- the opposite side, same line

-- The advice the site gave, as it stood when the match started - the CS2 twin
-- of aces.advice. At most one tip per bookmaker match and map (slot map1...),
-- the line the model rates best on that map: Betano's ladders offer a dozen
-- correlated lines per map, and they would otherwise all be tips on one view.
-- Rewritten by advice.py until kick-off, locked by the trigger below from then on.
CREATE TABLE IF NOT EXISTS cs2.advice (
  bookmaker        TEXT    NOT NULL,
  event_id         TEXT    NOT NULL,
  slot             TEXT    NOT NULL,     -- map1, map2, map3
  kickoff          TIMESTAMPTZ NOT NULL,
  league           TEXT,
  team1            TEXT    NOT NULL,     -- the bookmaker's names and order
  team2            TEXT    NOT NULL,
  map_num          INTEGER NOT NULL,
  market           TEXT    NOT NULL,     -- rounds, handicap, team_rounds
  team             INTEGER NOT NULL DEFAULT 0,
  side             TEXT    NOT NULL DEFAULT '',
  line             NUMERIC NOT NULL DEFAULT 0,
  odds             NUMERIC NOT NULL,
  p_model          NUMERIC NOT NULL,     -- the model's chance, blended 3:1 with Betano's no-vig one
  p_raw            NUMERIC NOT NULL,     -- the model's own chance
  edge             NUMERIC NOT NULL,     -- p_model * odds - 1
  rating           SMALLINT,             -- stake, tenths of a unit
  backed           BOOLEAN NOT NULL,     -- false: advised, then withdrawn before kick-off
  code_version     TEXT,
  first_advised_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  advised_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  result           TEXT,                 -- won | lost | void
  actual           INTEGER,
  settled_at       TIMESTAMPTZ,
  placed           BOOLEAN NOT NULL DEFAULT false,
  placed_at        TIMESTAMPTZ,
  PRIMARY KEY (bookmaker, event_id, slot),
  FOREIGN KEY (bookmaker, event_id) REFERENCES cs2.bk_event (bookmaker, event_id)
);
CREATE INDEX IF NOT EXISTS advice_kickoff_idx ON cs2.advice (kickoff);

CREATE OR REPLACE FUNCTION cs2.advice_locked() RETURNS trigger AS $$
BEGIN
  IF OLD.kickoff <= now() AND
     (NEW.market, NEW.team, NEW.side, NEW.line, NEW.odds, NEW.p_model, NEW.edge, NEW.backed,
      NEW.kickoff, NEW.rating)
       IS DISTINCT FROM
     (OLD.market, OLD.team, OLD.side, OLD.line, OLD.odds, OLD.p_model, OLD.edge, OLD.backed,
      OLD.kickoff, OLD.rating) THEN
    RAISE EXCEPTION 'cs2.advice %/%/% is locked: the match started at %',
      OLD.bookmaker, OLD.event_id, OLD.slot, OLD.kickoff;
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS advice_locked ON cs2.advice;
CREATE TRIGGER advice_locked BEFORE UPDATE ON cs2.advice
  FOR EACH ROW EXECUTE FUNCTION cs2.advice_locked();
CREATE OR REPLACE RULE advice_no_delete AS ON DELETE TO cs2.advice
  WHERE OLD.kickoff <= now() DO INSTEAD NOTHING;

-- What Telegram was last told about a tip (notify.py). Not part of the tip,
-- so the lock leaves these alone. told_bet is market|team|side|line.
ALTER TABLE cs2.advice ADD COLUMN IF NOT EXISTS told_bet    TEXT;
ALTER TABLE cs2.advice ADD COLUMN IF NOT EXISTS told_rating SMALLINT;
ALTER TABLE cs2.advice ADD COLUMN IF NOT EXISTS told_at     TIMESTAMPTZ;
