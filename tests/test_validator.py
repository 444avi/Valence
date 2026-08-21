"""Validation cache: hit/miss, resolution-text invalidation, real-call counting.

This is new, cost-bearing logic (the plan's highest-value item), so it gets the
only validator test in the suite. The model is never called for real here — a
counting stub stands in for `_call_model`, which lets us assert exactly how many
*real* calls each scenario incurs.
"""

import sqlite3

import pytest

from arb import valcache, validator
from arb.models import ArbOpportunity, CandidateMatch, Market, Validation


def _market(platform, mid, question, description):
    mid_id = "pm-slug" if platform == "polymarket" else "KX-TICKER"
    return Market(
        platform, mid_id, question, description,
        yes_indicative=mid, no_indicative=1 - mid,
        yes_ask=mid, no_ask=1 - mid, category="sports",
    )


def _opp(pm_q="A beats B", pm_desc="Resolves YES if A wins the match.",
         ks_q="Will A beat B?", ks_desc="Winner of the A-B match."):
    pm = _market("polymarket", 0.4, pm_q, pm_desc)
    ks = _market("kalshi", 0.55, ks_q, ks_desc)
    match = CandidateMatch(polymarket=pm, kalshi=ks, similarity=0.9)
    return ArbOpportunity(match=match, legs=[], cost=0.95, profit=0.05, roi=0.05)


def _verdict(reasoning="same event, hedged"):
    return Validation(same_event=True, equivalent_payoff=True,
                      confidence=0.9, reasoning=reasoning, caveats=["watch draws"])


class _CountingModel:
    """Stand-in for validator._call_model that records how often it fires."""

    def __init__(self, verdict):
        self.calls = 0
        self.verdict = verdict

    def __call__(self, opp, client=None):
        self.calls += 1
        return self.verdict


@pytest.fixture
def cache_env(tmp_path, monkeypatch):
    db = tmp_path / "valence.db"
    monkeypatch.setenv("VALENCE_DB", str(db))
    monkeypatch.delenv("VALENCE_RUN_ID", raising=False)
    monkeypatch.delenv("VALENCE_CACHE_TTL_DAYS", raising=False)
    return db


def test_no_env_means_no_cache_and_one_call_each(monkeypatch):
    """With VALENCE_DB unset, validate() is a plain passthrough to the model."""
    monkeypatch.delenv("VALENCE_DB", raising=False)
    stub = _CountingModel(_verdict())
    monkeypatch.setattr(validator, "_call_model", stub)

    opp = _opp()
    validator.validate(opp)
    validator.validate(opp)
    assert stub.calls == 2  # no cache => every call is real


def test_miss_then_hit(cache_env, monkeypatch):
    stub = _CountingModel(_verdict())
    monkeypatch.setattr(validator, "_call_model", stub)

    opp = _opp()
    first = validator.validate(opp)
    second = validator.validate(opp)  # identical pair -> cache hit

    assert stub.calls == 1
    assert second.same_event and second.equivalent_payoff
    assert second.reasoning == first.reasoning
    assert second.caveats == ["watch draws"]


def test_description_change_invalidates(cache_env, monkeypatch):
    """A resolution-text edit with an UNCHANGED title must miss the cache.

    This is the failure the full-text hash exists to prevent: a rules edit that
    re-scopes the market while leaving the question string identical.
    """
    stub = _CountingModel(_verdict())
    monkeypatch.setattr(validator, "_call_model", stub)

    validator.validate(_opp(ks_desc="Winner of the A-B match."))
    # Same questions and ids, only Kalshi's resolution rules changed.
    validator.validate(_opp(ks_desc="Winner incl. extra time and penalties."))

    assert stub.calls == 2  # both real: the edit is a genuine miss


def test_question_change_invalidates(cache_env, monkeypatch):
    stub = _CountingModel(_verdict())
    monkeypatch.setattr(validator, "_call_model", stub)

    validator.validate(_opp(pm_q="A beats B"))
    validator.validate(_opp(pm_q="Does A beat B in regulation?"))

    assert stub.calls == 2


def test_ttl_expiry_forces_revalidation(cache_env, monkeypatch):
    stub = _CountingModel(_verdict())
    monkeypatch.setattr(validator, "_call_model", stub)

    opp = _opp()
    validator.validate(opp)  # writes the row with created_at = now

    # Backdate the stored row well past a 30-day TTL, then set the TTL.
    conn = sqlite3.connect(str(cache_env))
    conn.execute("UPDATE validations SET created_at = '2000-01-01T00:00:00+00:00'")
    conn.commit()
    conn.close()
    monkeypatch.setenv("VALENCE_CACHE_TTL_DAYS", "30")

    validator.validate(opp)
    assert stub.calls == 2  # stale hit treated as a miss


def test_real_call_counter_tracks_misses_only(cache_env, monkeypatch):
    """runs.llm_calls counts cache misses (real API calls), not hits.

    A hit still yields a Validation object, so counting result objects would
    overstate spend; the counter must move only on the miss path.
    """
    # Stand up a minimal runs table like the web layer's, and point the cache at it.
    conn = sqlite3.connect(str(cache_env))
    conn.execute("CREATE TABLE runs (id TEXT PRIMARY KEY, llm_calls INTEGER DEFAULT 0)")
    conn.execute("INSERT INTO runs (id, llm_calls) VALUES ('run-1', 0)")
    conn.commit()
    conn.close()
    monkeypatch.setenv("VALENCE_RUN_ID", "run-1")

    stub = _CountingModel(_verdict())
    monkeypatch.setattr(validator, "_call_model", stub)

    opp = _opp()
    validator.validate(opp)  # miss -> real call, counter 1
    validator.validate(opp)  # hit  -> no call, counter unchanged
    validator.validate(_opp(pm_q="different question"))  # miss -> counter 2

    conn = sqlite3.connect(str(cache_env))
    (count,) = conn.execute("SELECT llm_calls FROM runs WHERE id='run-1'").fetchone()
    conn.close()
    assert count == 2
    assert stub.calls == 2


def test_question_hash_covers_all_four_fields():
    base = valcache.question_hash("q1", "d1", "q2", "d2")
    assert base != valcache.question_hash("Q1", "d1", "q2", "d2")
    assert base != valcache.question_hash("q1", "D1", "q2", "d2")
    assert base != valcache.question_hash("q1", "d1", "Q2", "d2")
    assert base != valcache.question_hash("q1", "d1", "q2", "D2")
    assert base == valcache.question_hash("q1", "d1", "q2", "d2")
