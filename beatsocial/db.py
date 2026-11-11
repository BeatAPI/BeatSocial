"""[INPUT] A data directory path.
[OUTPUT] SQLite connection with the BeatSocial schema applied; data-dir layout helpers.
[POS] Storage layer. Only core.py and legacy.py write through it; CLI and web go through core.py.
[PROTOCOL] Schema changes bump SCHEMA_VERSION and add an idempotent step in migrate().
"""
from __future__ import annotations

import os
import sqlite3

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS accounts (
  id TEXT PRIMARY KEY,               -- channel key, e.g. X-EN, TH-JP
  name TEXT NOT NULL,                -- display name, e.g. "X 日文"
  platform TEXT NOT NULL,            -- model.PLATFORMS key
  country TEXT NOT NULL,             -- model.COUNTRIES key
  lang TEXT NOT NULL,                -- en, ja, ko, ru, zh, zh-Hant
  handle TEXT,                       -- @handle, t.me/..., profile URL
  tz TEXT DEFAULT 'Asia/Shanghai',   -- target audience time zone (display only)
  bot TEXT,                          -- default publishing bot
  publish_method TEXT,               -- official API / cloud browser / manual ...
  per_week INTEGER DEFAULT 0,        -- planned posts per week
  slot_times TEXT DEFAULT '[]',      -- JSON list of "HH:MM" (Beijing time)
  types TEXT DEFAULT '[]',           -- JSON list of content types this account rotates
  rules TEXT,                        -- format rules: link placement, length, first comment
  status TEXT NOT NULL DEFAULT 'active',
  notes TEXT,
  created_at TEXT, updated_at TEXT
);

CREATE TABLE IF NOT EXISTS posts (
  id TEXT PRIMARY KEY,               -- W47-X-EN-03
  account_id TEXT NOT NULL REFERENCES accounts(id),
  week TEXT NOT NULL,                -- ISO week 2026-W47
  slot_at TEXT NOT NULL,             -- 'YYYY-MM-DD HH:MM' Beijing time
  type TEXT,                         -- content type, used by stats and quotas
  topic TEXT,
  product TEXT,
  source_kind TEXT DEFAULT 'original', -- original | benchmark | reuse | hot
  source_url TEXT,
  benchmark_id TEXT REFERENCES samples(id),
  status TEXT NOT NULL DEFAULT 'todo',
  current_v INTEGER DEFAULT 0,
  bot TEXT,                          -- overrides account bot
  ai_review TEXT,                    -- pass | fail | NULL
  approved_v INTEGER, approved_hash TEXT, approved_at TEXT, approved_by TEXT,
  price_checked_at TEXT,             -- when a person confirmed prices for the approved version
  claimed_by TEXT, claimed_at TEXT,
  published_url TEXT, published_at TEXT, published_by TEXT,
  legacy TEXT,                       -- original JSON row from the old plan.json
  created_at TEXT, updated_at TEXT
);
CREATE INDEX IF NOT EXISTS posts_week ON posts(week);
CREATE INDEX IF NOT EXISTS posts_account ON posts(account_id);
CREATE INDEX IF NOT EXISTS posts_status ON posts(status);

CREATE TABLE IF NOT EXISTS versions (
  post_id TEXT NOT NULL REFERENCES posts(id),
  v INTEGER NOT NULL,
  text TEXT NOT NULL DEFAULT '',     -- main body
  reply_thread TEXT DEFAULT '',      -- first reply / first comment / thread
  zh_ref TEXT DEFAULT '',            -- Chinese reference for review only
  writer_note TEXT DEFAULT '',
  author TEXT,
  created_at TEXT,
  PRIMARY KEY (post_id, v)
);

CREATE TABLE IF NOT EXISTS media (
  post_id TEXT NOT NULL REFERENCES posts(id),
  slot INTEGER NOT NULL,             -- 1, 2, 3 ...
  kind TEXT NOT NULL DEFAULT 'image',-- image | video
  spec TEXT DEFAULT '',              -- what the asset must show, size, ratio
  file TEXT,                         -- absolute path or path relative to data/media
  updated_at TEXT,
  PRIMARY KEY (post_id, slot)
);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  post_id TEXT,
  at TEXT NOT NULL,
  actor TEXT,
  action TEXT NOT NULL,              -- draft, edit, approve, reject, note, claim, receipt, metric, ai_review ...
  detail TEXT
);
CREATE INDEX IF NOT EXISTS events_post ON events(post_id);

CREATE TABLE IF NOT EXISTS metrics (
  post_id TEXT NOT NULL REFERENCES posts(id),
  period TEXT NOT NULL,              -- h24 | d7 | d30
  collected_at TEXT,
  source TEXT,
  impressions INTEGER, likes INTEGER, replies INTEGER, reposts INTEGER,
  quotes INTEGER, bookmarks INTEGER, clicks INTEGER, followers_delta INTEGER,
  extra TEXT,                        -- JSON of platform-specific fields
  PRIMARY KEY (post_id, period)
);

CREATE TABLE IF NOT EXISTS competitors (
  id TEXT PRIMARY KEY,               -- slug, e.g. fal
  name TEXT NOT NULL,
  why TEXT,                          -- why we benchmark them
  status TEXT DEFAULT 'active',
  notes TEXT,
  created_at TEXT
);

CREATE TABLE IF NOT EXISTS samples (
  id TEXT PRIMARY KEY,               -- B1-001 or S-<n>
  competitor_id TEXT REFERENCES competitors(id),
  author TEXT,
  platform TEXT, country TEXT,
  url TEXT UNIQUE,
  posted_at TEXT, observed_at TEXT,
  type TEXT,                         -- matches posts.type where possible
  for_account TEXT,                  -- which of our accounts it is a reference for
  hook TEXT,                         -- opening line
  structure TEXT,                    -- hook -> pain -> fix -> number ...
  cta TEXT, media_kind TEXT,
  impressions INTEGER, likes INTEGER, replies INTEGER, reposts INTEGER, bookmarks INTEGER,
  how_to_adapt TEXT,                 -- how we replace their facts with ours
  evidence_note TEXT,
  created_at TEXT
);

CREATE TABLE IF NOT EXISTS markets (
  code TEXT PRIMARY KEY,             -- GLOBAL, JP, KR, TW, RU, CN (= accounts.country)
  focus TEXT,                        -- what this market mainly promotes
  audience TEXT,                     -- who we talk to
  cta TEXT,                          -- where we send people
  notes TEXT,
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS rules (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  scope TEXT NOT NULL DEFAULT 'global', -- global | platform:x | country:JP | account:X-EN | type:<type>
  text TEXT NOT NULL,
  evidence TEXT,
  status TEXT NOT NULL DEFAULT 'proposed',
  proposed_by TEXT, proposed_at TEXT,
  decided_by TEXT, decided_at TEXT
);
"""


def layout(data_dir: str) -> dict:
    return {
        "db": os.path.join(data_dir, "beatsocial.db"),
        "media": os.path.join(data_dir, "media"),
        "memory": os.path.join(data_dir, "memory"),
        "playbooks": os.path.join(data_dir, "memory", "playbooks"),
        "retros": os.path.join(data_dir, "memory", "复盘"),
        "quota": os.path.join(data_dir, "memory", "quota"),
        "breakdowns": os.path.join(data_dir, "memory", "竞对拆解"),
        "work": os.path.join(data_dir, "work"),
        "backup": os.path.join(data_dir, "backup"),
    }


def connect(data_dir: str) -> sqlite3.Connection:
    paths = layout(data_dir)
    if not os.path.exists(paths["db"]):
        raise SystemExit(f"没有找到数据库：{paths['db']}。先运行 beatsocial init <数据目录>")
    return _open(paths["db"])


def _open(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def create(data_dir: str) -> sqlite3.Connection:
    paths = layout(data_dir)
    for key in ("media", "memory", "playbooks", "retros", "quota", "breakdowns", "work", "backup"):
        os.makedirs(paths[key], exist_ok=True)
    conn = _open(paths["db"])
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    from .model import MARKET_DEFAULTS

    conn.executescript(SCHEMA)
    for code, d in MARKET_DEFAULTS.items():  # seed once; never overwrite the owner's edits
        conn.execute("INSERT OR IGNORE INTO markets(code, focus, audience, cta, notes, updated_at) "
                     "VALUES(?,?,?,?,?,datetime('now','localtime'))",
                     (code, d["focus"], d["audience"], d["cta"], ""))
    conn.execute("UPDATE accounts SET country='GLOBAL' WHERE country='US'")  # US folded into the English market
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(SCHEMA_VERSION),),
    )
