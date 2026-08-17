"""Max position sizing: edge-exhaustion + depth ceiling (offline, injected books)."""

import math

from arb.fees import FeeConfig
from arb.models import ArbLeg, ArbOpportunity, CandidateMatch, Market
from arb.sizing import SizingConfig, max_position, _consume, _ks_ask_ladder


def _kalshi(mid=0.5):
    return Market("kalshi", "KXG-1", "A wins", "",
                  yes_indicative=mid, no_indicative=1 - mid,
                  yes_ask=mid, no_ask=1 - mid, category="sports")


def _pm(mid=0.5, fees_enabled=False):
    return Market("polymarket", "pm", "A wins", "",
                  yes_indicative=mid, no_indicative=1 - mid,
                  yes_ask=mid, no_ask=1 - mid, category="sports",
                  fees_enabled=fees_enabled, clob_tokens=("t_yes", "t_no"))


def _opp(yes_platform="kalshi"):
    """YES leg on `yes_platform`, NO leg on the other."""
    ks, pm = _kalshi(), _pm()
    match = CandidateMatch(polymarket=pm, kalshi=ks, similarity=1.0)
    if yes_platform == "kalshi":
        legs = [ArbLeg("kalshi", "YES", 0.40, 0.0),
                ArbLeg("polymarket", "NO", 0.45, 0.0)]
    else:
        legs = [ArbLeg("polymarket", "YES", 0.40, 0.0),
                ArbLeg("kalshi", "NO", 0.45, 0.0)]
    return ArbOpportunity(match=match, legs=legs, cost=0.85, profit=0.15, roi=0.1)


# Hand-computed example (zero fees): the spread hits 0 at size 160.
#   yes avg(s) = 0.6 - 20/s,  no avg(s) = 0.65 - 20/s   for 100 < s <= 200
#   spread(s) = 1 - yes - no = -0.25 + 40/s  ->  zero at s = 160.
_YES = [(0.40, 100.0), (0.60, 100.0)]
_NO = [(0.45, 100.0), (0.65, 100.0)]
_NOFEE = FeeConfig(kalshi_rate=0.0)   # + PM fees_enabled=False => zero fees


def test_edge_exhaustion_zero_crossing():
    r = max_position(_opp(), _NOFEE, SizingConfig(impact_buffer=1.0),
                     yes_ladder=_YES, no_ladder=_NO)
    assert r is not None
    assert math.isclose(r.edge_exhaustion_size, 160.0, abs_tol=0.5)
    # Spread is ~0 right at the exhaustion size.
    assert abs(r.spread_at_recommended) < 1e-3
    assert math.isclose(r.max_fillable, 200.0)


def test_impact_buffer_caps_below_edge():
    r = max_position(_opp(), _NOFEE, SizingConfig(impact_buffer=0.75),
                     yes_ladder=_YES, no_ladder=_NO)
    # depth up to the exhaustion marginal price is 200 on each leg; 75% => 150.
    assert math.isclose(r.depth_ceiling_size, 200.0)
    assert math.isclose(r.recommended_size, 150.0, abs_tol=0.5)
    assert r.recommended_size < r.edge_exhaustion_size
    assert any("buffer" in n for n in r.notes)


def test_fees_shrink_the_edge():
    with_fee = max_position(_opp(), FeeConfig(kalshi_rate=0.07),
                            SizingConfig(impact_buffer=1.0),
                            yes_ladder=_YES, no_ladder=_NO)
    assert with_fee.edge_exhaustion_size < 160.0   # fees eat into it


def test_fixed_costs_amortize_but_still_bound():
    r = max_position(_opp(), _NOFEE,
                     SizingConfig(impact_buffer=1.0, gas_cost=1.0,
                                  withdrawal_cost=1.0),
                     yes_ladder=_YES, no_ladder=_NO)
    # $2 fixed spread over the pairs; edge slightly below the frictionless 160.
    assert r.edge_exhaustion_size < 160.0
    assert r.profit_at_recommended > 0


def test_thinner_leg_bottlenecks():
    thin_no = [(0.45, 100.0), (0.65, 20.0)]   # only 120 total depth
    r = max_position(_opp(), _NOFEE, SizingConfig(impact_buffer=1.0),
                     yes_ladder=_YES, no_ladder=thin_no)
    assert math.isclose(r.max_fillable, 120.0)
    # Edge would be 160 unbounded, but only 120 pairs are fillable.
    assert r.edge_exhaustion_size <= 120.0 + 1e-6


def test_negative_from_top_of_book_is_zero():
    bad_yes = [(0.70, 100.0)]
    bad_no = [(0.70, 100.0)]      # 1.40 cost, never profitable
    r = max_position(_opp(), _NOFEE, SizingConfig(),
                     yes_ladder=bad_yes, no_ladder=bad_no)
    assert r.edge_exhaustion_size == 0.0
    assert r.recommended_size == 0.0


def test_kalshi_ask_ladder_inversion():
    # Buying YES consumes NO bids at ask = 1 - bid; best ask from highest bid.
    ob = {"orderbook_fp": {
        "no_dollars": [["0.40", "10"], ["0.53", "5"]],
        "yes_dollars": [["0.30", "8"]],
    }}

    class _Resp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return ob

    import arb.sizing as S
    orig = S.requests.get
    S.requests.get = lambda *a, **k: _Resp()
    try:
        yes = _ks_ask_ladder("KXG-1", is_yes=True)
        no = _ks_ask_ladder("KXG-1", is_yes=False)
    finally:
        S.requests.get = orig
    # YES asks: 1-0.53=0.47 (best), 1-0.40=0.60
    assert yes[0] == (0.47, 5.0) or math.isclose(yes[0][0], 0.47)
    assert math.isclose(no[0][0], 0.70)   # 1 - 0.30
