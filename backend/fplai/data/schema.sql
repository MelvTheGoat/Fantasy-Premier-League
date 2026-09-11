-- Schema for the FPL AI Manager.
--
-- Two ideas shape it:
--
--   * Anything that must not change once a deadline has passed lives in its
--     own table with the gameweek in the primary key -- `locked_picks`,
--     `projections`, `manager_state`. Rows there are written once and are
--     never regenerated, which is what keeps the backfill free of leakage.
--   * Anything the FPL API owns -- prices, points, fixtures -- is refreshed in
--     place, with a snapshot table where the history matters.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- --- Reference data from bootstrap-static ---------------------------------

-- Strength ratings are stored as the API sends them, but note what it sends
-- for 2026/27: `strength_overall_home` / `_away` are a 1-5 rating, and the
-- four granular attack/defence columns are zero for every club -- FPL stopped
-- publishing them. The projection model therefore derives attacking and
-- defensive strength from fixture difficulty and actual results, not from
-- these columns. They are kept so that if FPL starts populating them again,
-- ingestion picks them up without a migration.
CREATE TABLE IF NOT EXISTS teams (
    id              INTEGER PRIMARY KEY,
    code            INTEGER NOT NULL,   -- used to build shirt image URLs
    name            TEXT    NOT NULL,
    short_name      TEXT    NOT NULL,
    strength_overall_home INTEGER,
    strength_overall_away INTEGER,
    strength_attack_home  INTEGER,
    strength_attack_away  INTEGER,
    strength_defence_home INTEGER,
    strength_defence_away INTEGER
);

CREATE TABLE IF NOT EXISTS players (
    id              INTEGER PRIMARY KEY,   -- the API's `element` id
    code            INTEGER NOT NULL,
    web_name        TEXT    NOT NULL,
    first_name      TEXT,
    second_name     TEXT,
    team_id         INTEGER NOT NULL REFERENCES teams(id),
    -- Read from the API every refresh. Several players were reclassified for
    -- 2026/27, so this is never derived from a stored map.
    element_type    INTEGER NOT NULL,
    now_cost        INTEGER NOT NULL,      -- tenths of a million
    status          TEXT,                  -- a=available, i=injured, s=suspended, u=unavailable
    chance_of_playing_next_round INTEGER,
    chance_of_playing_this_round INTEGER,
    news            TEXT,
    news_added      TEXT,
    selected_by_percent REAL,
    updated_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_players_team ON players(team_id);
CREATE INDEX IF NOT EXISTS idx_players_type ON players(element_type);

-- Price history, so a purchase price can always be reconstructed and the
-- selling-price rule can be audited after the fact.
CREATE TABLE IF NOT EXISTS player_prices (
    player_id   INTEGER NOT NULL REFERENCES players(id),
    gameweek    INTEGER NOT NULL,
    now_cost    INTEGER NOT NULL,
    captured_at TEXT    NOT NULL,
    PRIMARY KEY (player_id, gameweek)
);

CREATE TABLE IF NOT EXISTS gameweeks (
    id                  INTEGER PRIMARY KEY,   -- the API's `event` id
    name                TEXT,
    deadline_time       TEXT    NOT NULL,
    is_current          INTEGER NOT NULL DEFAULT 0,
    is_next             INTEGER NOT NULL DEFAULT 0,
    finished            INTEGER NOT NULL DEFAULT 0,
    data_checked        INTEGER NOT NULL DEFAULT 0,
    -- The official gameweek average, straight from `average_entry_score`.
    average_entry_score INTEGER,
    highest_score       INTEGER,
    -- When the last fixture of the gameweek kicked off, used to derive the
    -- 09:00 UK lockdown after which scores stop being provisional.
    last_kickoff_time   TEXT,
    updated_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fixtures (
    id              INTEGER PRIMARY KEY,
    gameweek        INTEGER REFERENCES gameweeks(id),  -- NULL until scheduled
    kickoff_time    TEXT,
    team_h          INTEGER NOT NULL REFERENCES teams(id),
    team_a          INTEGER NOT NULL REFERENCES teams(id),
    team_h_difficulty INTEGER,
    team_a_difficulty INTEGER,
    team_h_score    INTEGER,
    team_a_score    INTEGER,
    started         INTEGER NOT NULL DEFAULT 0,
    finished        INTEGER NOT NULL DEFAULT 0,
    finished_provisional INTEGER NOT NULL DEFAULT 0,
    updated_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_fixtures_gameweek ON fixtures(gameweek);
CREATE INDEX IF NOT EXISTS idx_fixtures_teams ON fixtures(team_h, team_a);

-- --- Actual results -------------------------------------------------------

-- One row per player per fixture, from `event/{gw}/live/`. Keeping the fixture
-- in the key is what makes double gameweeks work: a player with two fixtures
-- has two rows that are summed when the gameweek is scored.
CREATE TABLE IF NOT EXISTS player_gameweek_stats (
    player_id       INTEGER NOT NULL REFERENCES players(id),
    gameweek        INTEGER NOT NULL,
    fixture_id      INTEGER NOT NULL,
    minutes         INTEGER NOT NULL DEFAULT 0,
    total_points    INTEGER NOT NULL DEFAULT 0,
    goals_scored    INTEGER NOT NULL DEFAULT 0,
    assists         INTEGER NOT NULL DEFAULT 0,
    clean_sheets    INTEGER NOT NULL DEFAULT 0,
    goals_conceded  INTEGER NOT NULL DEFAULT 0,
    own_goals       INTEGER NOT NULL DEFAULT 0,
    penalties_saved INTEGER NOT NULL DEFAULT 0,
    penalties_missed INTEGER NOT NULL DEFAULT 0,
    yellow_cards    INTEGER NOT NULL DEFAULT 0,
    red_cards       INTEGER NOT NULL DEFAULT 0,
    saves           INTEGER NOT NULL DEFAULT 0,
    bonus           INTEGER NOT NULL DEFAULT 0,
    bps             INTEGER NOT NULL DEFAULT 0,
    -- Defensive contribution: the raw actions and the 2 points if awarded.
    clearances_blocks_interceptions INTEGER NOT NULL DEFAULT 0,
    tackles         INTEGER NOT NULL DEFAULT 0,
    recoveries      INTEGER NOT NULL DEFAULT 0,
    defensive_contribution INTEGER NOT NULL DEFAULT 0,
    expected_goals  REAL    NOT NULL DEFAULT 0,
    expected_assists REAL   NOT NULL DEFAULT 0,
    expected_goal_involvements REAL NOT NULL DEFAULT 0,
    expected_goals_conceded REAL NOT NULL DEFAULT 0,
    starts          INTEGER NOT NULL DEFAULT 0,
    updated_at      TEXT    NOT NULL,
    PRIMARY KEY (player_id, gameweek, fixture_id)
);

CREATE INDEX IF NOT EXISTS idx_pgs_gameweek ON player_gameweek_stats(gameweek);

-- --- Projections ----------------------------------------------------------

-- Expected points, written once per (gameweek made, gameweek projected) pair.
-- `made_for_gameweek` is the deadline the projection was generated before, so
-- a backfill can prove that nothing after that deadline was used.
CREATE TABLE IF NOT EXISTS projections (
    player_id           INTEGER NOT NULL REFERENCES players(id),
    made_for_gameweek   INTEGER NOT NULL,
    target_gameweek     INTEGER NOT NULL,
    expected_points     REAL    NOT NULL,
    expected_minutes    REAL,
    probability_of_start REAL,
    expected_goals      REAL,
    expected_assists    REAL,
    clean_sheet_probability REAL,
    defcon_probability  REAL,
    expected_bonus      REAL,
    fixture_count       INTEGER NOT NULL DEFAULT 1,
    model_version       TEXT    NOT NULL,
    created_at          TEXT    NOT NULL,
    PRIMARY KEY (player_id, made_for_gameweek, target_gameweek)
);

CREATE INDEX IF NOT EXISTS idx_projections_target
    ON projections(made_for_gameweek, target_gameweek);

-- --- Model squads ---------------------------------------------------------

-- 'manager' or 'best_xi'. Kept as a table so a third model could be added
-- without a schema change.
CREATE TABLE IF NOT EXISTS models (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    description TEXT
);

INSERT OR IGNORE INTO models (id, name, description) VALUES
    ('manager', 'The Manager',
     'Carries one squad all season under real transfer, budget and chip constraints.'),
    ('best_xi', 'Best XI of the Week',
     'Rebuilds the best legal squad from scratch each gameweek, with no transfers or chips.');

-- The locked picks for one model in one gameweek. Written at the deadline and
-- never rewritten -- `locked_at` records when, and the API layer refuses to
-- overwrite a row that already exists.
CREATE TABLE IF NOT EXISTS locked_picks (
    model_id        TEXT    NOT NULL REFERENCES models(id),
    gameweek        INTEGER NOT NULL,
    player_id       INTEGER NOT NULL REFERENCES players(id),
    -- 1-11 start, 12-15 bench; 12 is always the substitute goalkeeper.
    squad_position  INTEGER NOT NULL,
    is_captain      INTEGER NOT NULL DEFAULT 0,
    is_vice_captain INTEGER NOT NULL DEFAULT 0,
    purchase_price  INTEGER NOT NULL,
    selling_price   INTEGER NOT NULL,
    -- Why this player is in the squad, shown behind a tap in the frontend.
    selection_reason TEXT,
    locked_at       TEXT    NOT NULL,
    PRIMARY KEY (model_id, gameweek, player_id)
);

CREATE INDEX IF NOT EXISTS idx_locked_picks_gw ON locked_picks(model_id, gameweek);

-- The Manager's carried state at each deadline: bank, free transfers, chip.
CREATE TABLE IF NOT EXISTS manager_state (
    gameweek            INTEGER PRIMARY KEY,
    bank                INTEGER NOT NULL,
    squad_value         INTEGER NOT NULL,
    free_transfers      INTEGER NOT NULL,
    free_transfers_after INTEGER NOT NULL,
    transfers_made      INTEGER NOT NULL DEFAULT 0,
    transfer_cost       INTEGER NOT NULL DEFAULT 0,
    chip                TEXT,
    chip_reason         TEXT,
    -- Set when the Manager deliberately held its transfer.
    roll_reason         TEXT,
    locked_at           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transfers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    gameweek        INTEGER NOT NULL,
    out_player_id   INTEGER NOT NULL REFERENCES players(id),
    in_player_id    INTEGER NOT NULL REFERENCES players(id),
    selling_price   INTEGER NOT NULL,
    purchase_price  INTEGER NOT NULL,
    -- The side-by-side comparison shown in the frontend, as JSON: projected
    -- points over the horizon for each player, fixtures, form, underlying
    -- stats, price and availability.
    comparison      TEXT    NOT NULL,
    reason          TEXT    NOT NULL,
    was_hit         INTEGER NOT NULL DEFAULT 0,
    locked_at       TEXT    NOT NULL,
    UNIQUE (gameweek, out_player_id, in_player_id)
);

CREATE INDEX IF NOT EXISTS idx_transfers_gw ON transfers(gameweek);

CREATE TABLE IF NOT EXISTS chips_used (
    chip        TEXT    NOT NULL,
    gameweek    INTEGER NOT NULL,
    chip_set    INTEGER NOT NULL,
    reason      TEXT,
    locked_at   TEXT    NOT NULL,
    PRIMARY KEY (chip, chip_set)
);

-- --- Results per model ----------------------------------------------------

CREATE TABLE IF NOT EXISTS gameweek_results (
    model_id            TEXT    NOT NULL REFERENCES models(id),
    gameweek            INTEGER NOT NULL,
    points_before_hits  INTEGER NOT NULL,
    transfer_cost       INTEGER NOT NULL DEFAULT 0,
    points              INTEGER NOT NULL,
    bench_points        INTEGER NOT NULL DEFAULT 0,
    captain_points      INTEGER NOT NULL DEFAULT 0,
    average_entry_score INTEGER,
    beat_average        INTEGER,
    -- 0 while the gameweek is still provisional, 1 once past lockdown.
    is_final            INTEGER NOT NULL DEFAULT 0,
    chip                TEXT,
    formation           TEXT,
    updated_at          TEXT    NOT NULL,
    PRIMARY KEY (model_id, gameweek)
);

-- Auto-substitutions actually applied, so the frontend can mark them.
CREATE TABLE IF NOT EXISTS auto_subs (
    model_id        TEXT    NOT NULL REFERENCES models(id),
    gameweek        INTEGER NOT NULL,
    out_player_id   INTEGER NOT NULL REFERENCES players(id),
    in_player_id    INTEGER NOT NULL REFERENCES players(id),
    PRIMARY KEY (model_id, gameweek, out_player_id)
);

-- --- Ingestion bookkeeping ------------------------------------------------

CREATE TABLE IF NOT EXISTS ingest_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    endpoint    TEXT    NOT NULL,
    gameweek    INTEGER,
    status      TEXT    NOT NULL,
    rows        INTEGER,
    message     TEXT,
    ran_at      TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT NOT NULL
);

INSERT OR IGNORE INTO schema_version (version, applied_at)
VALUES (1, datetime('now'));
