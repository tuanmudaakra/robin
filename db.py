"""
db.py — Inisialisasi SQLite, schema, dan helper akses data.

Tabel:
  wallets             Smart wallet registry (dinamis, v2.1)
  wallet_candidates   Hasil discovery sebelum dipromosikan
  wallet_trades       Transaksi on-chain terdeteksi
  call_history        Riwayat call + tracking harga untuk lesson-learn
  lessons             Pelajaran (post-mortem) per call + agregat
  darwin_state        Key-value adaptive (threshold, bobot)
  signals             Legacy (deprecated, dipertahankan agar DB lama tetap bisa dibuka)
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from config.settings import settings, log


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def get_conn():
    conn = sqlite3.connect(settings.db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS wallets (
    address TEXT PRIMARY KEY,
    label TEXT,
    tags TEXT,
    is_active INTEGER DEFAULT 1,
    added_at TEXT,
    last_scan_at TEXT,
    total_trades INTEGER DEFAULT 0,
    win_rate REAL,
    avg_roi REAL,
    source TEXT DEFAULT 'manual',
    discovery_score REAL,
    promoted_at TEXT,
    demoted_at TEXT
);

CREATE TABLE IF NOT EXISTS wallet_candidates (
    address TEXT PRIMARY KEY,
    discovery_score REAL,
    winner_tokens INTEGER DEFAULT 0,
    earliness_avg REAL,
    hit_rate REAL,
    est_roi REAL,
    sample_tokens TEXT,
    first_seen_at TEXT,
    last_eval_at TEXT,
    status TEXT DEFAULT 'PENDING'
);

CREATE TABLE IF NOT EXISTS wallet_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet_address TEXT,
    wallet_label TEXT,
    tx_signature TEXT UNIQUE,
    timestamp TEXT,
    trade_type TEXT,
    token_mint TEXT,
    token_symbol TEXT,
    token_amount REAL,
    sol_amount REAL,
    usd_value REAL,
    price_at_trade REAL,
    program TEXT,
    detected_at TEXT
);

CREATE TABLE IF NOT EXISTS call_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_mint TEXT,
    token_symbol TEXT,
    call_type TEXT,
    confidence INTEGER,
    price_at_call REAL,
    mc_at_call REAL,
    liquidity_at_call REAL,
    smart_wallet_buys INTEGER,
    cluster_buy INTEGER DEFAULT 0,
    reason TEXT,
    features TEXT,                 -- JSON snapshot fitur saat call (untuk lesson-learn)
    called_at TEXT,
    checked_at TEXT,
    price_now REAL,
    price_change_pct REAL,
    -- tracking sepanjang periode --
    peak_price REAL,
    peak_pct REAL,
    trough_price REAL,
    trough_pct REAL,
    period_end_at TEXT,
    final_price REAL,
    final_pct REAL,
    outcome TEXT DEFAULT 'PENDING',  -- HIT / MISS / UNKNOWN / PENDING
    hold_hours INTEGER DEFAULT 0,
    hit_streak INTEGER DEFAULT 0,
    UNIQUE(token_mint, called_at)
);

CREATE TABLE IF NOT EXISTS lessons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id INTEGER,
    token_symbol TEXT,
    outcome TEXT,
    kind TEXT,                    -- 'per_call' / 'aggregate'
    summary TEXT,
    detail TEXT,                  -- JSON: faktor menang/kalah
    source TEXT DEFAULT 'rule',   -- 'rule' / 'llm'
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS darwin_state (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_mint TEXT,
    signal_type TEXT,
    confidence INTEGER,
    created_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_trades_token ON wallet_trades(token_mint);
CREATE INDEX IF NOT EXISTS idx_trades_wallet ON wallet_trades(wallet_address);
CREATE INDEX IF NOT EXISTS idx_calls_outcome ON call_history(outcome);
CREATE INDEX IF NOT EXISTS idx_calls_token ON call_history(token_mint);
"""

# kolom yang mungkin perlu ditambahkan ke DB lama (idempotent migration)
_MIGRATIONS = {
    "wallets": {
        "source": "TEXT DEFAULT 'manual'",
        "discovery_score": "REAL",
        "promoted_at": "TEXT",
        "demoted_at": "TEXT",
    },
    "call_history": {
        "features": "TEXT",
        "peak_price": "REAL",
        "peak_pct": "REAL",
        "trough_price": "REAL",
        "trough_pct": "REAL",
        "period_end_at": "TEXT",
        "final_price": "REAL",
        "final_pct": "REAL",
    },
}


def _existing_cols(conn, table: str) -> set:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        # migrasi kolom yang hilang di DB lama
        for table, cols in _MIGRATIONS.items():
            have = _existing_cols(conn, table)
            for col, decl in cols.items():
                if col not in have:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
                    log.info("migration: %s.%s ditambahkan", table, col)
    log.info("DB siap di %s", settings.db_path)


# ----------------------------- wallets -----------------------------

def active_wallets() -> list[sqlite3.Row]:
    with get_conn() as conn:
        return list(conn.execute(
            "SELECT * FROM wallets WHERE is_active=1 ORDER BY COALESCE(win_rate, -1) DESC"))


def count_active_wallets() -> int:
    with get_conn() as conn:
        return conn.execute("SELECT COUNT(*) c FROM wallets WHERE is_active=1").fetchone()["c"]


def get_wallet(address: str) -> Optional[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM wallets WHERE address=?", (address,)).fetchone()


def upsert_wallet(address: str, label: str = "", tags: str = "", source: str = "manual",
                  discovery_score: Optional[float] = None) -> None:
    now = utcnow()
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO wallets(address, label, tags, is_active, added_at, source,
                                   discovery_score, promoted_at)
               VALUES(?,?,?,1,?,?,?,?)
               ON CONFLICT(address) DO UPDATE SET
                   is_active=1, label=excluded.label, tags=excluded.tags,
                   demoted_at=NULL""",
            (address, label, tags, now, source, discovery_score,
             now if source == "discovered" else None),
        )


def set_wallet_active(address: str, active: bool) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE wallets SET is_active=?, demoted_at=? WHERE address=?",
            (1 if active else 0, None if active else utcnow(), address),
        )


def update_wallet_stats(address: str, win_rate: float, avg_roi: float, total_trades: int) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE wallets SET win_rate=?, avg_roi=?, total_trades=?, last_scan_at=? WHERE address=?",
            (win_rate, avg_roi, total_trades, utcnow(), address),
        )


# ------------------------- wallet_candidates ------------------------

def upsert_candidate(row: dict) -> None:
    now = utcnow()
    with get_conn() as conn:
        exists = conn.execute(
            "SELECT first_seen_at FROM wallet_candidates WHERE address=?",
            (row["address"],)).fetchone()
        first_seen = exists["first_seen_at"] if exists else now
        conn.execute(
            """INSERT INTO wallet_candidates
                 (address, discovery_score, winner_tokens, earliness_avg, hit_rate,
                  est_roi, sample_tokens, first_seen_at, last_eval_at, status)
               VALUES(:address,:discovery_score,:winner_tokens,:earliness_avg,:hit_rate,
                      :est_roi,:sample_tokens,:first_seen_at,:last_eval_at,:status)
               ON CONFLICT(address) DO UPDATE SET
                  discovery_score=excluded.discovery_score,
                  winner_tokens=excluded.winner_tokens,
                  earliness_avg=excluded.earliness_avg,
                  hit_rate=excluded.hit_rate,
                  est_roi=excluded.est_roi,
                  sample_tokens=excluded.sample_tokens,
                  last_eval_at=excluded.last_eval_at,
                  status=excluded.status""",
            {
                "address": row["address"],
                "discovery_score": row.get("discovery_score"),
                "winner_tokens": row.get("winner_tokens", 0),
                "earliness_avg": row.get("earliness_avg"),
                "hit_rate": row.get("hit_rate"),
                "est_roi": row.get("est_roi"),
                "sample_tokens": json.dumps(row.get("sample_tokens", [])),
                "first_seen_at": first_seen,
                "last_eval_at": now,
                "status": row.get("status", "PENDING"),
            },
        )


def set_candidate_status(address: str, status: str) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE wallet_candidates SET status=?, last_eval_at=? WHERE address=?",
                     (status, utcnow(), address))


def get_candidate(address: str) -> Optional[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM wallet_candidates WHERE address=?", (address,)).fetchone()


def candidates_by_status(status: str) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return list(conn.execute(
            "SELECT * FROM wallet_candidates WHERE status=? ORDER BY discovery_score DESC", (status,)))


# ----------------------------- trades ------------------------------

def insert_trade(trade: dict) -> bool:
    """Return True kalau baru (tx_signature unik)."""
    cols = ("wallet_address", "wallet_label", "tx_signature", "timestamp", "trade_type",
            "token_mint", "token_symbol", "token_amount", "sol_amount", "usd_value",
            "price_at_trade", "program", "detected_at")
    vals = tuple(trade.get(c) for c in cols)
    with get_conn() as conn:
        try:
            conn.execute(
                f"INSERT INTO wallet_trades({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                vals)
            return True
        except sqlite3.IntegrityError:
            return False


def recent_trades(hours_back: int = 6) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return list(conn.execute(
            """SELECT * FROM wallet_trades
               WHERE detected_at >= datetime('now', ?)
               ORDER BY detected_at DESC""",
            (f"-{hours_back} hours",)))


# --------------------------- call_history --------------------------

def insert_call(call: dict) -> Optional[int]:
    now = utcnow()
    cols = ("token_mint", "token_symbol", "call_type", "confidence", "price_at_call",
            "mc_at_call", "liquidity_at_call", "smart_wallet_buys", "cluster_buy",
            "reason", "features", "called_at", "period_end_at", "outcome")
    data = {c: call.get(c) for c in cols}
    data["called_at"] = call.get("called_at", now)
    data["outcome"] = call.get("outcome", "PENDING")
    if isinstance(data.get("features"), (dict, list)):
        data["features"] = json.dumps(data["features"])
    with get_conn() as conn:
        try:
            cur = conn.execute(
                f"INSERT INTO call_history({','.join(cols)}) VALUES({','.join(':' + c for c in cols)})",
                data)
            return cur.lastrowid
        except sqlite3.IntegrityError:
            return None  # sudah pernah di-call pada waktu yang sama


def open_calls() -> list[sqlite3.Row]:
    """Call yang belum final (masih perlu tracking harga)."""
    with get_conn() as conn:
        return list(conn.execute(
            "SELECT * FROM call_history WHERE outcome IN ('PENDING') ORDER BY called_at"))


def update_call_tracking(call_id: int, fields: dict) -> None:
    if not fields:
        return
    sets = ", ".join(f"{k}=:{k}" for k in fields)
    fields = {**fields, "_id": call_id, "_checked": utcnow()}
    with get_conn() as conn:
        conn.execute(f"UPDATE call_history SET {sets}, checked_at=:_checked WHERE id=:_id", fields)


def call_stats() -> dict:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT outcome, COUNT(*) c FROM call_history GROUP BY outcome").fetchall()
    stats = {r["outcome"]: r["c"] for r in rows}
    stats["total"] = sum(stats.values())
    return stats


# --------------------------- lessons -------------------------------

def insert_lesson(call_id: Optional[int], symbol: str, outcome: str, kind: str,
                  summary: str, detail: Any, source: str = "rule") -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO lessons(call_id, token_symbol, outcome, kind, summary, detail, source, created_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (call_id, symbol, outcome, kind, summary,
             json.dumps(detail) if not isinstance(detail, str) else detail,
             source, utcnow()))


def recent_lessons(limit: int = 5, kind: str = "per_call") -> list[sqlite3.Row]:
    with get_conn() as conn:
        return list(conn.execute(
            "SELECT * FROM lessons WHERE kind=? ORDER BY id DESC LIMIT ?", (kind, limit)))


# --------------------------- darwin_state --------------------------

def get_state(key: str, default: Any = None) -> Any:
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM darwin_state WHERE key=?", (key,)).fetchone()
    if not row:
        return default
    try:
        return json.loads(row["value"])
    except (json.JSONDecodeError, TypeError):
        return row["value"]


def set_state(key: str, value: Any) -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO darwin_state(key, value, updated_at) VALUES(?,?,?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
            (key, json.dumps(value), utcnow()))


if __name__ == "__main__":
    init_db()
    print("call_stats:", call_stats())
    print("active wallets:", count_active_wallets())
