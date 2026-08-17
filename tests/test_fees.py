"""Fee-model correctness (defects 1 and 4)."""

import math

from arb.fees import FeeConfig, kalshi_fee, polymarket_fee, fee_for
from arb.models import Market


def _pm(category="crypto", fees_enabled=True):
    return Market(
        platform="polymarket", market_id="x", question="q", description="",
        yes_indicative=0.5, no_indicative=0.5, category=category,
        fees_enabled=fees_enabled,
    )


# --- Defect 1: Polymarket fee is a symmetric P*(1-P) curve, not linear ---

def test_polymarket_fee_near_zero_at_extreme():
    cfg = FeeConfig()
    fee_95 = polymarket_fee(0.95, cfg, category="crypto")
    fee_50 = polymarket_fee(0.50, cfg, category="crypto")
    # 0.07 * 0.95 * 0.05 = 0.003325  (near zero)
    assert math.isclose(fee_95, 0.07 * 0.95 * 0.05, rel_tol=1e-9)
    assert fee_95 < 0.005
    # Peaks at 0.50: 0.07 * 0.25 = 0.0175 — far larger than at 0.95.
    assert fee_50 > fee_95 * 4
    # The OLD linear model (rate*price) would have charged ~0.0665 here — near
    # its maximum. Confirm we are nowhere near that.
    assert fee_95 < 0.07 * 0.95 / 4


def test_polymarket_fee_zero_when_fees_disabled():
    cfg = FeeConfig()
    assert polymarket_fee(0.50, cfg, category="crypto", fees_enabled=False) == 0.0
    # And via fee_for on a Market with feesEnabled=false, any category.
    assert fee_for(_pm("crypto", fees_enabled=False), 0.50, cfg) == 0.0


def test_polymarket_rate_is_per_category():
    cfg = FeeConfig()
    # politics 0.04, sports 0.05, crypto 0.07 at the same price.
    p = 0.50
    assert polymarket_fee(p, cfg, category="politics") == 0.04 * 0.25
    assert polymarket_fee(p, cfg, category="sports") == 0.05 * 0.25
    assert polymarket_fee(p, cfg, category="crypto") == 0.07 * 0.25


def test_polymarket_rate_override_replaces_table():
    cfg = FeeConfig(polymarket_rate_override=0.10)
    # Every category now uses 0.10.
    for cat in ("politics", "sports", "crypto", "unknown"):
        assert polymarket_fee(0.50, cfg, category=cat) == 0.10 * 0.25


# --- Defect 4: Kalshi fee is rounded once per ORDER, not per contract ---

def test_kalshi_fee_per_order_rounding():
    cfg = FeeConfig()
    # 0.07 * 100 * 0.30 * 0.70 = 1.47 exactly; one ceil -> 1.47, not 2.00.
    assert kalshi_fee(0.30, cfg, contracts=100) == 1.47


def test_kalshi_per_contract_upper_bound_at_size_one():
    cfg = FeeConfig()
    # Single contract rounds up to a full cent (upper bound).
    one = kalshi_fee(0.30, cfg, contracts=1)
    assert one == math.ceil(0.07 * 0.30 * 0.70 * 100) / 100  # 0.02
    # Per-contract cost is strictly lower at size 100.
    assert kalshi_fee(0.30, cfg, contracts=100) / 100 < one


def test_kalshi_series_rate_override():
    cfg = FeeConfig(kalshi_series_rates={"KXNFLGAME": 0.035})
    assert cfg.kalshi_rate_for("KXNFLGAME-26XYZ") == 0.035
    assert cfg.kalshi_rate_for("KXWCGAME-26XYZ") == 0.07
