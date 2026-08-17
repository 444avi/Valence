"""Platform fee models.

Verified against live fee documentation (August 2026):
  - Kalshi:     ceil(rate * C * P * (1-P)) computed ONCE per order (not per
                contract), rate 0.07 for takers with a per-series multiplier
                that currently defaults to 1. https://kalshi.com/fee-schedule
  - Polymarket: fee = shares * rate * P * (1-P), a symmetric curve peaking at
                P=0.50 (NOT linear in price). Rate is per-category; makers pay
                0; only markets with feesEnabled=true are charged.
                https://startpolymarket.com/learn/polymarket-fees/

Both are estimates — re-verify before trading real money.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Mapping, Optional

# Polymarket taker fee rate by our canonical section (see arb/categories.py).
# geopolitics/world markets are fee-free on Polymarket; they fold into our
# `politics` bucket here, but such markets carry feesEnabled=false and so are
# already zero-rated by the feesEnabled gate regardless of this table.
POLYMARKET_FEE_RATES: dict[str, float] = {
    "politics": 0.04,
    "finance": 0.04,
    "tech": 0.04,
    "sports": 0.05,
    "culture": 0.05,
    "crypto": 0.07,
}
POLYMARKET_DEFAULT_RATE = 0.05


@dataclass(frozen=True)
class FeeConfig:
    # --- Kalshi ---
    kalshi_rate: float = 0.07
    # Optional per-series overrides keyed by ticker prefix (e.g. "KXNFLGAME").
    # Empty => every series uses kalshi_rate. All series known to us are 0.07.
    kalshi_series_rates: Mapping[str, float] = field(default_factory=dict)

    # --- Polymarket ---
    # Per-category rate table. `polymarket_rate_override`, when set (via the
    # --polymarket-fee CLI flag), replaces EVERY category with that flat rate.
    polymarket_rates: Mapping[str, float] = field(
        default_factory=lambda: dict(POLYMARKET_FEE_RATES)
    )
    polymarket_default: float = POLYMARKET_DEFAULT_RATE
    polymarket_rate_override: Optional[float] = None

    def kalshi_rate_for(self, ticker: str) -> float:
        for prefix, rate in self.kalshi_series_rates.items():
            if ticker.startswith(prefix):
                return rate
        return self.kalshi_rate

    def polymarket_rate_for(self, category: str) -> float:
        if self.polymarket_rate_override is not None:
            return self.polymarket_rate_override
        return self.polymarket_rates.get(category, self.polymarket_default)


def kalshi_fee(
    price: float, cfg: FeeConfig, contracts: int = 1, ticker: str = ""
) -> float:
    """Total Kalshi fee for an order of `contracts` at `price` dollars.

    The ceil-to-cent is applied ONCE to the whole order, per Kalshi's schedule —
    not per contract. Rounding per contract overstates the fee (100 @ $0.30 is
    $1.47, not $2.00).
    """
    rate = cfg.kalshi_rate_for(ticker)
    raw = rate * contracts * price * (1.0 - price)
    return math.ceil(raw * 100.0) / 100.0


def polymarket_fee(
    price: float,
    cfg: FeeConfig,
    contracts: int = 1,
    category: str = "",
    fees_enabled: bool = True,
) -> float:
    """Total Polymarket fee for `contracts` shares at `price` dollars.

    Symmetric curve `shares * rate * P * (1-P)` (near zero at the extremes),
    and exactly zero when the market has fees disabled.
    """
    if not fees_enabled:
        return 0.0
    rate = cfg.polymarket_rate_for(category)
    return contracts * rate * price * (1.0 - price)


def fee_for(market, price: float, cfg: FeeConfig, contracts: int = 1) -> float:
    """Total order fee for buying `contracts` of `market` at `price`."""
    if market.platform == "kalshi":
        return kalshi_fee(price, cfg, contracts, market.market_id)
    return polymarket_fee(
        price, cfg, contracts, market.category, market.fees_enabled
    )
