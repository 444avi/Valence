"""Compute concrete arbitrage opportunities from matched pairs, net of fees.

Cross-platform arb on a binary YES/NO event: buy YES on one platform and NO on
the other. Whichever way the event resolves, exactly one leg pays $1, so the
position is risk-free if the combined cost (prices + fees) is below $1.

  Direction A: Polymarket YES + Kalshi NO
  Direction B: Kalshi YES + Polymarket NO

This assumes both markets resolve on the *same* underlying outcome with YES/NO
meaning the same thing — which is exactly what the LLM validator checks.
"""

from __future__ import annotations

from .fees import FeeConfig, fee_for
from .models import ArbLeg, ArbOpportunity, CandidateMatch


def _direction(
    match: CandidateMatch,
    yes_platform: str,
    cfg: FeeConfig,
    contracts: int,
    haircut: float,
) -> ArbOpportunity | None:
    pm, ks = match.polymarket, match.kalshi
    if yes_platform == "polymarket":
        yes_m, no_m = pm, ks
    else:
        yes_m, no_m = ks, pm

    yes_price, yes_conf = yes_m.buy_yes(haircut)
    no_price, no_conf = no_m.buy_no(haircut)
    if yes_price is None or no_price is None:
        return None

    # Fees are computed for the whole order, then expressed per contract so the
    # cost lines up with the per-contract $1 payoff. Kalshi's single per-order
    # ceil means the per-contract fee shrinks as size grows.
    yes_fee = fee_for(yes_m, yes_price, cfg, contracts) / contracts
    no_fee = fee_for(no_m, no_price, cfg, contracts) / contracts
    cost = yes_price + no_price + yes_fee + no_fee
    profit = 1.0 - cost
    roi = profit / cost if cost > 0 else 0.0

    legs = [
        ArbLeg(yes_m.platform, "YES", yes_price, yes_fee, yes_conf),
        ArbLeg(no_m.platform, "NO", no_price, no_fee, no_conf),
    ]
    # A Kalshi leg rounded at size 1 is an upper bound on the real per-contract
    # fee (the per-order ceil is amortized once real size is known).
    kalshi_bound = contracts == 1 and any(l.platform == "kalshi" for l in legs)
    return ArbOpportunity(
        match=match, legs=legs, cost=cost, profit=profit, roi=roi,
        contracts=contracts, confirmed=(yes_conf and no_conf),
        kalshi_fee_is_bound=kalshi_bound,
    )


def best_opportunity(
    match: CandidateMatch,
    cfg: FeeConfig,
    contracts: int = 1,
    haircut: float = 0.02,
) -> ArbOpportunity | None:
    """Return the more profitable of the two arb directions, if any priced."""
    candidates = [
        _direction(match, "polymarket", cfg, contracts, haircut),
        _direction(match, "kalshi", cfg, contracts, haircut),
    ]
    candidates = [c for c in candidates if c is not None]
    if not candidates:
        return None
    return max(candidates, key=lambda o: o.profit)


def find_opportunities(
    matches: list[CandidateMatch],
    cfg: FeeConfig,
    min_profit: float = 0.0,
    contracts: int = 1,
    haircut: float = 0.02,
) -> list[ArbOpportunity]:
    """Compute the best arb per match and keep those above `min_profit`."""
    opps: list[ArbOpportunity] = []
    for match in matches:
        opp = best_opportunity(match, cfg, contracts, haircut)
        if opp is not None and opp.profit > min_profit:
            opps.append(opp)
    opps.sort(key=lambda o: o.profit, reverse=True)
    return opps
