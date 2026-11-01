"""cost-report ledger: where the SQLite ledger lives, its schema, and how a request is priced.
Design: ../design.md (sections 4 and 5).

    <ledger> = $COST_LEDGER_DB, else $CLAUDE_CONFIG_DIR/cost/ledger.db, else ~/.claude/cost/ledger.db

The ledger stores token counts only. Cost is computed at query time by the SQL function
COST_SQL, which prices each row with the prices.json entry in force at the row's timestamp, so a
price change needs no re-ingest. No prompt or response text is ever stored.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import sqlite3
import time
from functools import lru_cache
from pathlib import Path
from typing import Any

PRICES_FILE = Path(__file__).resolve().parent / "prices.json"
BUSY_TIMEOUT_MS = 3000
MODEL_SUFFIX_RX = re.compile(r"(\[[^\]]*\])$|(-\d{8})$")  # 'claude-opus-5-5[1m]', 'claude-haiku-4-5-20251001'
TOKEN_COLS = ("input_tokens", "output_tokens", "thinking_tokens", "cache_read", "cache_write_5m",
              "cache_write_1h", "web_search", "web_fetch")

# Price of one request row aliased `r`; NULL when its model has no price.
COST_SQL = ("cost(r.model, r.ts, r.speed, r.input_tokens, r.output_tokens, r.cache_read, "
            "r.cache_write_5m, r.cache_write_1h, r.web_search)")

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
  request_id      TEXT PRIMARY KEY,     -- Anthropic requestId: one row per API call
  ts              TEXT NOT NULL,        -- ISO-8601 UTC of the first content block
  session_id      TEXT NOT NULL,
  agent_id        TEXT,                 -- NULL = the session's main thread
  model           TEXT,
  effort          TEXT,
  speed           TEXT,
  skill           TEXT,                 -- skill active when the call was made
  input_tokens    INTEGER NOT NULL DEFAULT 0,
  output_tokens   INTEGER NOT NULL DEFAULT 0,  -- includes thinking
  thinking_tokens INTEGER NOT NULL DEFAULT 0,
  cache_read      INTEGER NOT NULL DEFAULT 0,
  cache_write_5m  INTEGER NOT NULL DEFAULT 0,
  cache_write_1h  INTEGER NOT NULL DEFAULT 0,
  web_search      INTEGER NOT NULL DEFAULT 0,
  web_fetch       INTEGER NOT NULL DEFAULT 0,
  final_usage     INTEGER NOT NULL DEFAULT 0,  -- 1 = a row with stop_reason (or the handback) was seen
  cc_version      TEXT,
  source          TEXT NOT NULL         -- 'transcript' | 'handback'
);
CREATE INDEX IF NOT EXISTS ix_requests_session ON requests(session_id);
CREATE INDEX IF NOT EXISTS ix_requests_ts ON requests(ts);
CREATE INDEX IF NOT EXISTS ix_requests_agent ON requests(agent_id);

CREATE TABLE IF NOT EXISTS sessions (
  session_id   TEXT PRIMARY KEY,
  project      TEXT,                    -- last path component of cwd
  cwd          TEXT,
  git_branch   TEXT,
  entrypoint   TEXT,
  title        TEXT,
  first_ts     TEXT,
  last_ts      TEXT,
  cc_total_usd REAL,                    -- Claude Code's own session cost (status line), if seen
  cc_total_ts  TEXT
);

CREATE TABLE IF NOT EXISTS agents (
  agent_id         TEXT PRIMARY KEY,
  session_id       TEXT,
  parent_agent_id  TEXT,                -- NULL = spawned by the main thread
  agent_type       TEXT,
  description      TEXT,
  spawn_depth      INTEGER,
  requested_model  TEXT,                -- the Agent tool's alias, e.g. 'sonnet'
  resolved_model   TEXT,
  tool_use_id      TEXT,
  tool_uses        INTEGER,
  final_input      INTEGER,             -- the subagent's final request, from the parent's tool result
  final_cache_read INTEGER,
  final_output     INTEGER,
  final_thinking   INTEGER
);
CREATE INDEX IF NOT EXISTS ix_agents_session ON agents(session_id);

CREATE TABLE IF NOT EXISTS cc_reported (  -- Claude Code's own totals ('cost-state' rows)
  session_id   TEXT NOT NULL,
  model        TEXT NOT NULL,
  input_tokens INTEGER, output_tokens INTEGER, cache_read INTEGER, cache_write INTEGER,
  cost_usd     REAL,
  PRIMARY KEY (session_id, model)
);

CREATE TABLE IF NOT EXISTS limit_snapshots (
  ts         TEXT NOT NULL,             -- ISO-8601 UTC when recorded
  limit_name TEXT NOT NULL,             -- five_hour | seven_day | spend_limit
  used_pct   REAL,
  resets_at  TEXT,                      -- ISO-8601 UTC
  used_usd   REAL,
  limit_usd  REAL,
  session_id TEXT,
  PRIMARY KEY (ts, limit_name)
);

CREATE TABLE IF NOT EXISTS anomalies (
  path   TEXT NOT NULL,
  kind   TEXT NOT NULL,                 -- bad_json | no_usage | synthetic
  count  INTEGER NOT NULL,
  PRIMARY KEY (path, kind)
);

CREATE TABLE IF NOT EXISTS ingest_state (
  path   TEXT PRIMARY KEY,
  offset INTEGER NOT NULL,              -- bytes consumed (whole lines only)
  size   INTEGER NOT NULL,
  mtime  REAL NOT NULL
);
"""


def config_dir() -> Path:
    if cfg := os.environ.get("CLAUDE_CONFIG_DIR"):
        return Path(cfg)
    return Path.home() / ".claude"


def ledger_path() -> Path:
    if env := os.environ.get("COST_LEDGER_DB"):
        return Path(env)
    return config_dir() / "cost" / "ledger.db"


def _enable_wal(con: sqlite3.Connection) -> None:
    """Switch to WAL, retrying: SQLite fails this at once when writers race on a new ledger, without
    calling the busy handler. Past the busy timeout the error is raised."""
    deadline = time.monotonic() + BUSY_TIMEOUT_MS / 1000
    while True:
        try:
            con.execute("PRAGMA journal_mode=WAL")
            return
        except sqlite3.OperationalError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.05)


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=BUSY_TIMEOUT_MS / 1000)
    con.row_factory = sqlite3.Row
    con.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    _enable_wal(con)  # parallel sessions and hooks read while one writes
    con.executescript(SCHEMA)
    con.create_function("cost", 9, request_cost, deterministic=True)
    return con


# =========================================================================== pricing
@lru_cache(maxsize=1)
def prices() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(PRICES_FILE.read_text(encoding="utf-8"))
    return data


def normalize_model(model: str | None) -> str | None:
    """'claude-haiku-4-5-20251001' and 'claude-opus-5-5[1m]' -> their price-table ids."""
    if not model:
        return None
    return MODEL_SUFFIX_RX.sub("", model)


def rates_at(model: str | None, ts: str | None) -> dict[str, Any] | None:
    """The price entry for a model in force at a timestamp, or None when unpriced."""
    entries = prices()["models"].get(normalize_model(model) or "")
    if not entries:
        return None
    day = (ts or "")[:10] or dt.date.today().isoformat()
    in_force = [e for e in entries if e.get("from") is None or e["from"] <= day]
    if not in_force:
        return None
    return max(in_force, key=lambda e: e.get("from") or "")


def request_cost(model: str | None, ts: str | None, speed: str | None, input_tokens: int,
                 output_tokens: int, cache_read: int, cache_write_5m: int, cache_write_1h: int,
                 web_search: int) -> float | None:
    """USD for one request at the list price in force at `ts`. None when the model is unpriced."""
    r = rates_at(model, ts)
    if r is None:
        return None
    total = (
        (input_tokens or 0) * r["input"]
        + (output_tokens or 0) * r["output"]
        + (cache_read or 0) * r["cache_read"]
        + (cache_write_5m or 0) * r["cache_write_5m"]
        + (cache_write_1h or 0) * r["cache_write_1h"]
    ) / 1_000_000
    if speed == "fast":
        total *= r.get("fast_multiplier", 1.0)
    return total + (web_search or 0) * prices()["server_tools"]["web_search_per_request"]


def verification(model: str | None) -> str | None:
    """How the model's current price was verified: claude-code | docs | assumed | None (unpriced)."""
    r = rates_at(model, None)
    return None if r is None else str(r.get("verified", "assumed"))
