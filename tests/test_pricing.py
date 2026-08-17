"""Two-phase pricing and confirmation (defect 2) and validator scope (defect 5)."""

from arb.arbitrage import best_opportunity
from arb.fees import FeeConfig
from arb.models import CandidateMatch, Market, Validation


def _pm(yes_mid, no_mid, category="sports"):
    return Market(
        platform="polymarket", market_id="pm", question="Team A wins",
        description="", yes_indicative=yes_mid, no_indicative=no_mid,
        category=category,
    )


def _ks(yes_ask, no_ask):
    return Market(
        platform="kalshi", market_id="KXG-1", question="Team A wins",
        description="", yes_indicative=yes_ask, no_indicative=no_ask,
        yes_ask=yes_ask, no_ask=no_ask, category="sports",
    )


# --- Defect 2: indicative vs confirmed ---

def test_polymarket_leg_starts_unconfirmed():
    pm = _pm(0.30, 0.70)
    assert pm.tradeable()          # screenable on indicative
    assert not pm.confirmed        # no executable ask yet
    price, conf = pm.buy_yes(haircut=0.02)
    assert price == 0.32 and conf is False   # mid + haircut, unconfirmed


def test_kalshi_leg_is_confirmed_from_the_start():
    ks = _ks(0.55, 0.47)
    assert ks.confirmed
    price, conf = ks.buy_no(haircut=0.02)
    assert price == 0.47 and conf is True    # real ask, no haircut


def test_positive_edge_from_mids_is_not_realized():
    """Acceptance: a profit computed from indicative mids alone is NOT a
    realized arbitrage (the report must not present it as realizable)."""
    match = CandidateMatch(polymarket=_pm(0.30, 0.70), kalshi=_ks(0.55, 0.40),
                           similarity=0.9)
    opp = best_opportunity(match, FeeConfig(), contracts=1, haircut=0.02)
    assert opp.profit > 0            # looks profitable...
    assert opp.confirmed is False    # ...but a PM leg is only a mid+haircut
    assert opp.realized is False     # so it does NOT count as an arb


def test_confirmation_flips_to_realized():
    match = CandidateMatch(polymarket=_pm(0.30, 0.70), kalshi=_ks(0.55, 0.40),
                           similarity=0.9)
    # Simulate phase 2: CLOB best asks arrive.
    match.polymarket.yes_ask = 0.31
    match.polymarket.no_ask = 0.71
    opp = best_opportunity(match, FeeConfig(), contracts=1, haircut=0.02)
    assert opp.confirmed is True
    assert opp.realized is (opp.profit > 0)


def test_size_lowers_per_contract_kalshi_fee_flag():
    match = CandidateMatch(polymarket=_pm(0.30, 0.70), kalshi=_ks(0.55, 0.40),
                           similarity=0.9)
    one = best_opportunity(match, FeeConfig(), contracts=1)
    hundred = best_opportunity(match, FeeConfig(), contracts=100)
    assert one.kalshi_fee_is_bound is True       # size 1 => upper bound
    assert hundred.kalshi_fee_is_bound is False   # real size => not a bound
    # Per-contract profit improves with size (fee amortized).
    assert hundred.profit >= one.profit


# --- Defect 5: validator judges resolution only, not arithmetic ---

def test_validation_has_no_arbitrage_field():
    v = Validation(same_event=True, equivalent_payoff=True, confidence=0.9,
                   reasoning="", caveats=[])
    assert not hasattr(v, "arbitrage_exists")
    assert v.passed is True   # passed == same_event AND equivalent_payoff


def test_validator_schema_excludes_arbitrage_exists():
    from arb.validator import _SCHEMA
    assert "arbitrage_exists" not in _SCHEMA["properties"]
    assert "arbitrage_exists" not in _SCHEMA["required"]
    assert set(_SCHEMA["required"]) == {
        "same_event", "equivalent_payoff", "confidence", "reasoning", "caveats",
    }
