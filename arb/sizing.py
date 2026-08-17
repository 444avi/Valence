"""Max executable position sizing for a cross-platform arbitrage.

A positive fee-adjusted spread at top-of-book says an arb *exists*; it says
nothing about how much you can actually put on. This walks BOTH order books
level by level and answers "how many contract-pairs can I execute before the
edge is gone", bounded by two ceilings:

1. Edge-exhaustion ceiling — as size grows you eat deeper (worse) levels, so the
   average execution price on each leg rises. At each size we recompute the
   fee-adjusted spread from the *average* fill prices (net of Kalshi trading
   fees and Polymarket trading + fixed gas/withdrawal costs). The size at which
   that spread crosses zero is the edge-exhaustion ceiling.

2. Depth ceiling (thinner leg) — the total depth available on each leg at or
   better than the edge-exhaustion marginal price; the arb is bottlenecked by
   whichever leg is thinner, so we take the min. A configurable market-impact
   buffer caps usage at a fraction of that (default 75%) rather than 100%.

Order-book mechanics:
  * Polymarket: CLOB /book asks for the leg's token are direct ask levels.
  * Kalshi: to BUY YES you take the resting NO bids (ask = 1 - no_bid); to BUY
    NO you take the resting YES bids (ask = 1 - yes_bid).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import requests

from .fees import FeeConfig, kalshi_fee, polymarket_fee
from .models import ArbOpportunity, Market
from .sources import KALSHI_API_BASE, POLYMARKET_CLOB_BASE

_TIMEOUT = 15

# An order book as (ask_price, size) levels, sorted best (lowest) price first.
Ladder = list[tuple[float, float]]


@dataclass(frozen=True)
class SizingConfig:
    impact_buffer: float = 0.75      # cap usage at this fraction of thin-leg depth
    gas_cost: float = 0.0            # fixed $ per round-trip (Polygon gas), amortized
    withdrawal_cost: float = 0.0     # fixed $ per round-trip (USDC withdrawal)


@dataclass
class LegFill:
    platform: str
    side: str
    avg_price: float        # average execution price at the recommended size
    marginal_price: float   # worst (deepest) level touched
    depth: float            # total contracts available up to the marginal price


@dataclass
class SizingResult:
    edge_exhaustion_size: float   # pairs where the fee-adjusted spread hits 0
    depth_ceiling_size: float     # min available depth of the two legs (pre-buffer)
    recommended_size: float       # min(edge ceiling, buffer * depth ceiling)
    spread_at_recommended: float  # fee-adjusted spread per pair at that size
    profit_at_recommended: float  # total net profit (dollars) at that size
    max_fillable: float           # min total book depth across the two legs
    yes_leg: LegFill
    no_leg: LegFill
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------- order books

def _to_float(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _pm_ask_ladder(token_id: str) -> Ladder:
    try:
        resp = requests.get(
            f"{POLYMARKET_CLOB_BASE}/book",
            params={"token_id": token_id}, timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        asks = resp.json().get("asks", [])
    except (requests.RequestException, ValueError):
        return []
    out: Ladder = []
    for a in asks:
        p, s = _to_float(a.get("price")), _to_float(a.get("size"))
        if p is not None and s and 0.0 < p < 1.0 and s > 0:
            out.append((p, s))
    out.sort(key=lambda lv: lv[0])
    return out


def _ks_ask_ladder(ticker: str, is_yes: bool) -> Ladder:
    try:
        resp = requests.get(
            f"{KALSHI_API_BASE}/markets/{ticker}/orderbook", timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        ob = resp.json().get("orderbook_fp") or resp.json().get("orderbook") or {}
    except (requests.RequestException, ValueError):
        return []
    # Buying YES consumes resting NO bids; buying NO consumes resting YES bids.
    bids = ob.get("no_dollars" if is_yes else "yes_dollars") \
        or ob.get("no" if is_yes else "yes") or []
    out: Ladder = []
    for level in bids:
        bid, size = _to_float(level[0]), _to_float(level[1])
        if bid is None or size is None or size <= 0:
            continue
        if bid > 1.0:            # legacy cents
            bid /= 100.0
        ask = 1.0 - bid
        if 0.0 < ask < 1.0:
            out.append((ask, size))
    out.sort(key=lambda lv: lv[0])
    return out


def _leg_ladder(market: Market, is_yes: bool) -> Ladder:
    if market.platform == "polymarket":
        if len(market.clob_tokens) != 2:
            return []
        return _pm_ask_ladder(market.clob_tokens[0 if is_yes else 1])
    return _ks_ask_ladder(market.market_id, is_yes)


# ------------------------------------------------------------------ walking

def _consume(ladder: Ladder, size: float) -> tuple[float, float]:
    """Total $ cost and marginal price to buy `size` by eating the ladder."""
    remaining, cost, marginal = size, 0.0, (ladder[0][0] if ladder else 0.0)
    for price, avail in ladder:
        if remaining <= 1e-12:
            break
        take = min(remaining, avail)
        cost += take * price
        marginal = price
        remaining -= take
    return cost, marginal


def _total_depth(ladder: Ladder) -> float:
    return sum(sz for _, sz in ladder)


def _leg_fee(market: Market, avg: float, size: float, cfg: FeeConfig) -> float:
    if market.platform == "kalshi":
        return kalshi_fee(avg, cfg, contracts=size, ticker=market.market_id)
    return polymarket_fee(avg, cfg, contracts=size,
                          category=market.category, fees_enabled=market.fees_enabled)


def _spread_at(
    size: float, yes_m: Market, no_m: Market,
    yes_ladder: Ladder, no_ladder: Ladder, cfg: FeeConfig, scfg: SizingConfig,
) -> tuple[float, float]:
    """(fee-adjusted spread per pair, total net profit) for `size` pairs."""
    if size <= 0:
        return 0.0, 0.0
    cy, _ = _consume(yes_ladder, size)
    cn, _ = _consume(no_ladder, size)
    avg_y, avg_n = cy / size, cn / size
    fee = (_leg_fee(yes_m, avg_y, size, cfg)
           + _leg_fee(no_m, avg_n, size, cfg)
           + scfg.gas_cost + scfg.withdrawal_cost)
    total_cost = cy + cn + fee
    spread = 1.0 - total_cost / size          # per pair, $1 payoff
    return spread, size - total_cost


# ----------------------------------------------------------------- public

def max_position(
    opp: ArbOpportunity,
    cfg: FeeConfig,
    scfg: SizingConfig | None = None,
    yes_ladder: Ladder | None = None,
    no_ladder: Ladder | None = None,
) -> SizingResult | None:
    """Compute the max executable size for `opp`. Fetches both order books
    unless ladders are supplied (ladders make it network-free for testing)."""
    scfg = scfg or SizingConfig()
    match = opp.match
    yes_leg, no_leg = opp.legs[0], opp.legs[1]
    yes_m = match.polymarket if yes_leg.platform == "polymarket" else match.kalshi
    no_m = match.polymarket if no_leg.platform == "polymarket" else match.kalshi

    if yes_ladder is None:
        yes_ladder = _leg_ladder(yes_m, True)
    if no_ladder is None:
        no_ladder = _leg_ladder(no_m, False)
    if not yes_ladder or not no_ladder:
        return None

    max_fillable = min(_total_depth(yes_ladder), _total_depth(no_ladder))
    if max_fillable <= 0:
        return None

    def spread(s: float) -> float:
        return _spread_at(s, yes_m, no_m, yes_ladder, no_ladder, cfg, scfg)[0]

    # Edge-exhaustion is the LARGEST size at which the fee-adjusted spread is
    # still positive. Fixed costs (gas/withdrawal) and Kalshi's ceil-to-cent
    # make very small sizes unprofitable too, so the curve can be negative /
    # positive / negative — we can't assume monotonicity from the top of book.
    # Scan a fine grid, take the last positive step, then bisect to the exact
    # upper zero-crossing between it and the next (negative) step.
    steps = 2000
    grid = [max_fillable * i / steps for i in range(1, steps + 1)]
    last_pos_i = -1
    for i, s in enumerate(grid):
        if spread(s) > 0:
            last_pos_i = i
    if last_pos_i < 0:
        edge = 0.0                                   # never profitable at any size
    elif last_pos_i == steps - 1:
        edge = max_fillable                          # book runs out before edge does
    else:
        lo, hi = grid[last_pos_i], grid[last_pos_i + 1]
        for _ in range(60):                          # bisect to the crossing
            mid = (lo + hi) / 2.0
            if spread(mid) > 0:
                lo = mid
            else:
                hi = mid
        edge = lo

    # Depth ceiling: available depth up to the edge-exhaustion marginal price on
    # each leg, bottlenecked by the thinner one.
    _, my = _consume(yes_ladder, edge) if edge > 0 else (0.0, yes_ladder[0][0])
    _, mn = _consume(no_ladder, edge) if edge > 0 else (0.0, no_ladder[0][0])
    depth_y = sum(sz for p, sz in yes_ladder if p <= my + 1e-9)
    depth_n = sum(sz for p, sz in no_ladder if p <= mn + 1e-9)
    depth_ceiling = min(depth_y, depth_n)

    recommended = min(edge, scfg.impact_buffer * depth_ceiling)
    sp, profit = _spread_at(recommended, yes_m, no_m, yes_ladder, no_ladder,
                            cfg, scfg) if recommended > 0 else (0.0, 0.0)

    cy, myr = _consume(yes_ladder, recommended)
    cn, mnr = _consume(no_ladder, recommended)
    notes: list[str] = []
    if edge >= max_fillable:
        notes.append("edge never exhausts within visible depth; book-limited")
    if recommended < edge:
        notes.append(f"impact buffer capped size at {scfg.impact_buffer:.0%} "
                     "of thinner-leg depth")

    return SizingResult(
        edge_exhaustion_size=edge,
        depth_ceiling_size=depth_ceiling,
        recommended_size=recommended,
        spread_at_recommended=sp,
        profit_at_recommended=profit,
        max_fillable=max_fillable,
        yes_leg=LegFill(yes_leg.platform, "YES",
                        (cy / recommended) if recommended > 0 else 0.0,
                        myr, depth_y),
        no_leg=LegFill(no_leg.platform, "NO",
                       (cn / recommended) if recommended > 0 else 0.0,
                       mnr, depth_n),
        notes=notes,
    )
