CREATE TABLE IF NOT EXISTS analysis_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp           TEXT    NOT NULL,
    symbol              TEXT    NOT NULL,
    timeframe           TEXT    NOT NULL,
    signal              TEXT    NOT NULL,
    signal_source       TEXT,
    htf_bias            TEXT,
    entry_limit_price   REAL,
    entry_low           REAL,
    entry_high          REAL,
    stop_loss           REAL,
    hard_sl_price       REAL,
    take_profit_1       REAL,
    take_profit_2       REAL,
    invalidation_level  REAL,
    rrr                 REAL,
    reject_reason       TEXT,
    structure_description TEXT,
    executed            INTEGER DEFAULT 0,
    skip_reason         TEXT,
    config_version      TEXT,
    price_at_signal     REAL,
    outcome_checked     INTEGER DEFAULT 0,
    outcome_result      TEXT,
    outcome_checked_at  TEXT
);

CREATE TABLE IF NOT EXISTS trade_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    analysis_id     INTEGER NOT NULL,
    symbol          TEXT    NOT NULL,
    side            TEXT,
    notional_usd    REAL,
    fill_price      REAL,
    limit_price     REAL,
    stop_loss       REAL,
    hard_sl_price   REAL,
    server_stop_order_id TEXT,
    take_profit     REAL,
    open_time       TEXT,
    close_time      TEXT,
    close_price     REAL,
    pnl_usd         REAL,
    close_reason    TEXT,
    broker_order_id TEXT,
    forced_close_pnl_at_trigger REAL,
    adopted         INTEGER DEFAULT 0,
    FOREIGN KEY (analysis_id) REFERENCES analysis_log(id)
);

CREATE TABLE IF NOT EXISTS circuit_breaker_state (
    id          INTEGER PRIMARY KEY DEFAULT 1,
    state       TEXT    NOT NULL DEFAULT 'CLOSED',
    loss_streak INTEGER NOT NULL DEFAULT 0,
    opened_at   TEXT,
    updated_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS pending_orders (
    order_id      TEXT    PRIMARY KEY,
    side          TEXT,
    notional_usd  REAL,
    submitted_at  TEXT    NOT NULL,
    confirmed     INTEGER DEFAULT 0,
    confirmed_at  TEXT
);

CREATE TABLE IF NOT EXISTS system_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT NOT NULL,
    event_type  TEXT,
    message     TEXT,
    resolved    INTEGER DEFAULT 0
);
