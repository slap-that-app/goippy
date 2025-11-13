-- SQLite schema for goippy (experimental)

CREATE TABLE IF NOT EXISTS goippy_gateways (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ext TEXT UNIQUE,
    goip_id TEXT,
    goip_pass TEXT,
    channel INTEGER,
    msisdn TEXT,
    allow_regex TEXT,
    enabled INTEGER DEFAULT 1,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS goippy_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER,
    goip_id TEXT,
    ext TEXT,
    direction INTEGER,
    remote_num TEXT,
    cause TEXT,
    msisdn TEXT,
    raw TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS goippy_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dir TEXT,
    msisdn TEXT,
    xmpp_jid TEXT,
    body TEXT,
    status TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
