"""SQLite access for run metadata and the month-to-date usage query.

One writer (this process), a handful of readers; WAL mode handles it (plan §3).
The `validations` cache table is owned by arb/valcache.py — this module only
reads it for /usage — but we create it here too so a brand-new database has both
tables from first boot, before any job has run the validator.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional

from . import config

_BUSY_TIMEOUT_MS = 5000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id           TEXT PRIMARY KEY,
    type         TEXT NOT NULL,           -- 'scan' | 'max'
    args         TEXT NOT NULL,           -- JSON of the flag set used
    status       TEXT NOT NULL,           -- queued|running|done|failed|cancelled
    started_at   TEXT NOT NULL,           -- ISO8601
    finished_at  TEXT,
    exit_code    INTEGER,
    launched_by  TEXT NOT NULL,
    account_id   TEXT,
    result_path  TEXT,
    llm_calls    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS validations (
    pm_id         TEXT NOT NULL,
    ks_ticker     TEXT NOT NULL,
    question_hash TEXT NOT NULL,
    verdict       TEXT NOT NULL,
    reasoning     TEXT NOT NULL,
    model         TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    PRIMARY KEY (pm_id, ks_ticker, question_hash)
);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, timeout=_BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
    return conn


def init_db() -> None:
    config.ensure_dirs()
    conn = connect()
    try:
        conn.executescript(_SCHEMA)
        columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(runs)").fetchall()
        }
        if "account_id" not in columns:
            conn.execute("ALTER TABLE runs ADD COLUMN account_id TEXT")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_runs_account_id_started_at "
            "ON runs (account_id, started_at DESC)"
        )
        conn.commit()
    finally:
        conn.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def insert_run(
    run_id: str,
    run_type: str,
    args_json: str,
    account_id: str,
    launched_by: str,
) -> None:
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO runs "
            "(id, type, args, status, started_at, account_id, launched_by) "
            "VALUES (?, ?, ?, 'running', ?, ?, ?)",
            (run_id, run_type, args_json, _now(), account_id, launched_by),
        )
        conn.commit()
    finally:
        conn.close()


def finish_run(run_id: str, status: str, exit_code: Optional[int],
               result_path: Optional[str]) -> None:
    conn = connect()
    try:
        conn.execute(
            "UPDATE runs SET status=?, finished_at=?, exit_code=?, result_path=? "
            "WHERE id=?",
            (status, _now(), exit_code, result_path, run_id),
        )
        conn.commit()
    finally:
        conn.close()


def set_status(run_id: str, status: str) -> None:
    conn = connect()
    try:
        conn.execute("UPDATE runs SET status=? WHERE id=?", (status, run_id))
        conn.commit()
    finally:
        conn.close()


def get_run(run_id: str) -> Optional[dict[str, Any]]:
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def claim_legacy_runs(account_id: str, email: str) -> int:
    """One-time claim of email-only history by a verified Arboretum identity.

    The null predicate is the safety boundary: an existing stable owner is
    never changed, including when two account IDs present the same email.
    """
    conn = connect()
    try:
        cursor = conn.execute(
            "UPDATE runs SET account_id=? "
            "WHERE account_id IS NULL AND launched_by = ? COLLATE NOCASE",
            (account_id, email.strip()),
        )
        conn.commit()
        return cursor.rowcount
    finally:
        conn.close()


def list_runs(limit: int = 100, account_id: Optional[str] = None) -> list[dict[str, Any]]:
    conn = connect()
    try:
        if account_id is None:
            rows = conn.execute(
                "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM runs WHERE account_id = ? "
                "ORDER BY started_at DESC LIMIT ?",
                (account_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def list_run_accounts() -> list[dict[str, str]]:
    """Stable account IDs and current display emails for the admin filter."""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT account_id, MAX(LOWER(TRIM(launched_by))) AS email "
            "FROM runs WHERE account_id IS NOT NULL "
            "GROUP BY account_id ORDER BY email, account_id"
        ).fetchall()
        return [
            {"account_id": str(row["account_id"]), "email": str(row["email"])}
            for row in rows
        ]
    finally:
        conn.close()


def active_run() -> Optional[dict[str, Any]]:
    """The currently running (or queued) job, if any — the single-slot guard."""
    conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM runs WHERE status IN ('queued','running') "
            "ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def last_successful_run_at() -> Optional[str]:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT finished_at FROM runs WHERE status='done' "
            "ORDER BY finished_at DESC LIMIT 1"
        ).fetchone()
        return row["finished_at"] if row else None
    finally:
        conn.close()


def month_to_date_llm_calls(account_id: Optional[str] = None) -> int:
    """Sum *real* validator calls on runs started this month.

    Each cache miss increments its run's ``llm_calls`` counter in the same
    transaction that stores the validation. Summing those counters is both the
    cost truth and the only way to attribute usage to the user who launched it.
    ``account_id=None`` returns the all-account total.
    """
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    conn = connect()
    try:
        if account_id is None:
            (count,) = conn.execute(
                "SELECT COALESCE(SUM(llm_calls), 0) FROM runs WHERE started_at >= ?",
                (month_start.isoformat(),),
            ).fetchone()
        else:
            (count,) = conn.execute(
                "SELECT COALESCE(SUM(llm_calls), 0) FROM runs "
                "WHERE started_at >= ? AND account_id = ?",
                (month_start.isoformat(), account_id),
            ).fetchone()
        return int(count or 0)
    finally:
        conn.close()
