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
    realized arbitrage (the report must not present it as realizable).

    Both markets are "Team A wins" priced consistently (same polarity, a small
    real edge), so the reversed-polarity guard leaves the pair alone."""
    match = CandidateMatch(polymarket=_pm(0.44, 0.56), kalshi=_ks(0.50, 0.47),
                           similarity=0.9)
    opp = best_opportunity(match, FeeConfig(), contracts=1, haircut=0.02)
    assert opp.profit > 0            # looks profitable...
    assert opp.confirmed is False    # ...but a PM leg is only a mid+haircut
    assert opp.realized is False     # so it does NOT count as an arb


def test_confirmation_flips_to_realized():
    match = CandidateMatch(polymarket=_pm(0.44, 0.56), kalshi=_ks(0.50, 0.47),
                           similarity=0.9)
    # Simulate phase 2: CLOB best asks arrive (near the screening mids).
    match.polymarket.yes_ask = 0.45
    match.polymarket.no_ask = 0.55
    opp = best_opportunity(match, FeeConfig(), contracts=1, haircut=0.02)
    assert opp.confirmed is True
    assert opp.realized is (opp.profit > 0)


def test_size_lowers_per_contract_kalshi_fee_flag():
    match = CandidateMatch(polymarket=_pm(0.44, 0.56), kalshi=_ks(0.50, 0.47),
                           similarity=0.9)
    one = best_opportunity(match, FeeConfig(), contracts=1)
    hundred = best_opportunity(match, FeeConfig(), contracts=100)
    assert one.kalshi_fee_is_bound is True       # size 1 => upper bound
    assert hundred.kalshi_fee_is_bound is False   # real size => not a bound
    # Per-contract profit improves with size (fee amortized).
    assert hundred.profit >= one.profit


# --- Reversed-polarity guard: same event, opposite sides is NOT a hedge ---

def test_reversed_polarity_pair_yields_no_arb():
    """Regression (Wisconsin governor): Polymarket "Will the Democrats win?"
    (YES $0.805 / NO $0.195) paired with Kalshi "Will the Republican party win?"
    (YES $0.22 / NO $0.79). Same event, opposite sides — Kalshi-YES means the
    same thing as Polymarket-NO, so "buy Kalshi YES + Polymarket NO" doubles one
    bet rather than hedging. The naive math shows a fat ~$0.55 phantom profit;
    the engine must refuse to price it as an arbitrage."""
    pm = Market("polymarket", "pm", "Will the Democrats win the Wisconsin governor race?",
                "", yes_indicative=0.805, no_indicative=0.195,
                yes_ask=0.805, no_ask=0.195, category="politics")
    ks = Market("kalshi", "GOVPARTYWI-26-R",
                "Wisconsin Governor winner? Tom Tiffany", "",
                yes_indicative=0.22, no_indicative=0.79,
                yes_ask=0.22, no_ask=0.79, category="politics")
    match = CandidateMatch(polymarket=pm, kalshi=ks, similarity=0.48)
    assert best_opportunity(match, FeeConfig(), contracts=1) is None


def test_same_polarity_lopsided_arb_still_found():
    """The guard must not suppress a genuine arb just because the event is
    lopsided: two markets on the SAME side of an ~80/20 event, priced
    consistently, with a small real edge — still a hedge, still reported."""
    pm = Market("polymarket", "pm", "Team A wins", "",
                yes_indicative=0.76, no_indicative=0.24,
                yes_ask=0.76, no_ask=0.24, category="sports")
    ks = Market("kalshi", "KXA-1", "Team A wins", "",
                yes_indicative=0.80, no_indicative=0.19,
                yes_ask=0.80, no_ask=0.19, category="sports")
    match = CandidateMatch(polymarket=pm, kalshi=ks, similarity=0.9)
    opp = best_opportunity(match, FeeConfig(), contracts=1)
    assert opp is not None
    # Buy the cheaper YES (PM 0.76) + the other NO (KS 0.19) = 0.95 + fees < $1.
    assert opp.profit > 0
    assert {leg.side for leg in opp.legs} == {"YES", "NO"}


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
