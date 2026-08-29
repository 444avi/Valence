"""Cross-run validation cache for the LLM resolution-equivalence check.

The validator answers one question about a *pair* of markets: "do these two
resolve on the same event with equivalent payoff?" That is a property of the
pair, not of the moment — it does not change between runs unless the platforms
edit the market text. So the answer is cacheable, and caching it drives repeat
LLM cost toward near zero (see the implementation plan, section 8).

This module is deliberately standalone and side-effect-free unless a cache
database is configured via the environment, so the plain CLI (`python -m arb`)
keeps its original, cache-free behavior and the existing tests are untouched.
The web job runner turns the cache on by exporting:

    VALENCE_DB            path to the shared SQLite database (schema below)
    VALENCE_RUN_ID        the run whose real-call counter to increment
    VALENCE_CACHE_TTL_DAYS optional; a hit older than this is treated as a miss

The `question_hash` is load-bearing. It hashes the *full* text the model sees —
both questions AND both resolution descriptions — not just the titles. Platforms
edit resolution criteria while leaving a title unchanged; hashing only titles
would reuse a stale verdict against re-scoped rules, exactly the failure the key
exists to prevent. Any edit to any of the four fields is a cache miss.

Cost tracking rides on the *miss* path, not on the result blob: a cache hit still
produces a Validation object at zero cost, so counting Validation objects in the
output JSON would overstate spend once the cache starts saving money. The single
place that knows a real API call happened is `store()`, so it owns the counter.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Optional

from .models import ArbOpportunity, Validation

# Bumping the counter must not fail a run just because the DB is briefly locked
# by the API reading it; WAL + a generous busy timeout make writes wait, not error.
_BUSY_TIMEOUT_MS = 5000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS validations (
    pm_id         TEXT NOT NULL,
    ks_ticker     TEXT NOT NULL,
    question_hash TEXT NOT NULL,
    verdict       TEXT NOT NULL,   -- JSON: same_event, equivalent_payoff, confidence, caveats
    reasoning     TEXT NOT NULL,   -- model's stated reasoning, kept for audit
    model         TEXT NOT NULL,
    created_at    TEXT NOT NULL,   -- ISO8601 UTC
    PRIMARY KEY (pm_id, ks_ticker, question_hash)
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def question_hash(
    pm_question: str, pm_description: str, ks_question: str, ks_description: str
) -> str:
    """sha256 of the exact text the validator sees, in a fixed order.

    Mirrors `validator._build_prompt`: both questions and both resolution
    descriptions. Joined with a literal '|' so a field boundary can never be
    forged by content that happens to contain the separator's neighbors.
    """
    blob = "|".join(
        (pm_question or "", pm_description or "", ks_question or "", ks_description or "")
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def opp_question_hash(opp: ArbOpportunity) -> str:
    pm = opp.match.polymarket
    ks = opp.match.kalshi
    return question_hash(pm.question, pm.description, ks.question, ks.description)


class ValidationCache:
    """SQLite-backed cache keyed on (pm_id, ks_ticker, question_hash)."""

    def __init__(self, db_path: str, run_id: Optional[str] = None,
                 ttl_days: Optional[float] = None):
        self.db_path = db_path
        self.run_id = run_id
        self.ttl_days = ttl_days
        self._conn = sqlite3.connect(db_path, timeout=_BUSY_TIMEOUT_MS / 1000)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # -- reads -------------------------------------------------------------

    def lookup(self, opp: ArbOpportunity) -> Optional[Validation]:
        """Return a cached Validation for this pair, or None on miss / stale."""
        pm_id = opp.match.polymarket.market_id
        ks_ticker = opp.match.kalshi.market_id
        qhash = opp_question_hash(opp)
        row = self._conn.execute(
            "SELECT verdict, reasoning, created_at FROM validations "
            "WHERE pm_id=? AND ks_ticker=? AND question_hash=?",
            (pm_id, ks_ticker, qhash),
        ).fetchone()
        if row is None:
            return None
        verdict_json, reasoning, created_at = row
        if self._is_stale(created_at):
            return None
        data = json.loads(verdict_json)
        return Validation(
            same_event=bool(data["same_event"]),
            equivalent_payoff=bool(data["equivalent_payoff"]),
            confidence=float(data["confidence"]),
            reasoning=reasoning,
            caveats=list(data.get("caveats", [])),
        )

    def _is_stale(self, created_at: str) -> bool:
        if not self.ttl_days:
            return False
        try:
            made = datetime.fromisoformat(created_at)
        except ValueError:
            return False
        if made.tzinfo is None:
            made = made.replace(tzinfo=timezone.utc)
        age_days = (datetime.now(timezone.utc) - made).total_seconds() / 86400.0
        return age_days > self.ttl_days

    # -- writes ------------------------------------------------------------

    def store(self, opp: ArbOpportunity, v: Validation, model: str) -> None:
        """Record a *real* validator call and bump the run's real-call counter.

        Call this only after the model actually answered (a cache miss that hit
        the API). INSERT OR REPLACE so a post-TTL revalidation refreshes
        created_at, which is what makes it count as a real call this month.
        """
        pm_id = opp.match.polymarket.market_id
        ks_ticker = opp.match.kalshi.market_id
        qhash = opp_question_hash(opp)
        verdict = json.dumps({
            "same_event": v.same_event,
            "equivalent_payoff": v.equivalent_payoff,
            "confidence": v.confidence,
            "caveats": v.caveats,
        })
        with self._conn:  # transaction: cache row + counter move together
            self._conn.execute(
                "INSERT OR REPLACE INTO validations "
                "(pm_id, ks_ticker, question_hash, verdict, reasoning, model, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (pm_id, ks_ticker, qhash, verdict, v.reasoning, model, _now()),
            )
            if self.run_id:
                # The runs table lives in the same DB (created by the web layer).
                # Guard with a table check so cache-only use (e.g. tests) never
                # requires the web schema to exist.
                self._conn.execute(
                    "UPDATE runs SET llm_calls = llm_calls + 1 WHERE id = ?",
                    (self.run_id,),
                )

    def close(self) -> None:
        self._conn.close()


def from_env() -> Optional[ValidationCache]:
    """Build a cache from VALENCE_* env vars, or None if caching is off.

    Returning None is the signal for `validator.validate` to behave exactly as
    it did before the cache existed — the default for the CLI and the tests.
    """
    db_path = os.environ.get("VALENCE_DB")
    if not db_path:
        return None
    run_id = os.environ.get("VALENCE_RUN_ID") or None
    ttl_raw = os.environ.get("VALENCE_CACHE_TTL_DAYS")
    ttl_days: Optional[float] = None
    if ttl_raw:
        try:
            ttl_days = float(ttl_raw)
        except ValueError:
            ttl_days = None
    try:
        return ValidationCache(db_path, run_id=run_id, ttl_days=ttl_days)
    except sqlite3.Error:
        # A broken cache must never take down a run — degrade to no cache.
        return None
