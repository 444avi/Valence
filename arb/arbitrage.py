"""Compute concrete arbitrage opportunities from matched pairs, net of fees.

Cross-platform arb on a binary YES/NO event: buy YES on one platform and NO on
the other. Whichever way the event resolves, exactly one leg pays $1, so the
position is risk-free if the combined cost (prices + fees) is below $1.

  Direction A: Polymarket YES + Kalshi NO
  Direction B: Kalshi YES + Polymarket NO

Both directions assume the two markets are phrased with the SAME YES-polarity,
i.e. "YES here" and "YES there" mean the same real-world outcome, so YES+NO is a
hedge. The matcher only guarantees the pair is the same *topic*, not the same
polarity: it happily pairs Polymarket "Will the Democrats win?" with Kalshi
"Will the Republican party win?" — the SAME event phrased from OPPOSITE sides.
There, Kalshi-YES ("Republican wins") means the SAME thing as Polymarket-NO
("Democrats lose"), so "buy Kalshi YES + Polymarket NO" is one directional bet
bought twice (2x exposure), NOT a hedge — and its cost (two cheap same-side
legs) looks like a huge phantom arb. `_reversed_polarity` catches this from the
prices and declines the pair; see `best_opportunity`.
"""

from __future__ import annotations

from .fees import FeeConfig, fee_for
from .models import ArbLeg, ArbOpportunity, CandidateMatch

# Reversed-polarity guard (see module docstring). Two markets on the same event
# price the SAME outcome consistently, so the four indicative quotes fit either
# the same-side pairing (pm_yes≈ks_yes) or the reversed one (pm_yes≈ks_no). When
# the reversed fit is clearly better, the matcher has paired opposite sides and
# YES+NO is not a hedge. Only decidable when the event is lopsided: near 50/50 a
# genuine arb and a reversed listing yield identical prices, so we abstain there
# and leave the call to the LLM validator.
_POLARITY_MARGIN = 0.10   # reversed must fit the quotes this much better (dollars)
_POLARITY_FLAT = 0.15     # abstain unless a side sits >this far from 0.50


def _reversed_polarity(match: CandidateMatch) -> bool:
    """True when the two matched markets are the same event phrased from OPPOSITE
    sides, so buying YES on one and NO on the other doubles a bet instead of
    hedging it. Judged purely from the indicative quotes (always present for a
    tradeable pair); returns False whenever it cannot decide."""
    pm, ks = match.polymarket, match.kalshi
    y1, n1 = pm.yes_indicative, pm.no_indicative
    y2, n2 = ks.yes_indicative, ks.no_indicative
    if None in (y1, n1, y2, n2):
        return False
    # Near a coin flip the two hypotheses are numerically indistinguishable
    # (a real arb looks exactly like a reversed listing); don't guess.
    if abs(y1 - 0.5) < _POLARITY_FLAT:
        return False
    same_err = abs(y1 - y2) + abs(n1 - n2)   # fit if YES means the same thing
    rev_err = abs(y1 - n2) + abs(n1 - y2)     # fit if the sides are reversed
    return (same_err - rev_err) > _POLARITY_MARGIN


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
    """Return the more profitable of the two arb directions, if any priced.

    Returns None when the pair is the same event phrased from opposite sides
    (`_reversed_polarity`): there is no YES+NO hedge to price, and the apparent
    "profit" from the two same-side legs is a phantom, not risk-free money.
    """
    if _reversed_polarity(match):
        return None
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
