"""Shared data structures for markets, matches, and arbitrage results."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class Market:
    """A normalized binary (YES/NO) market from either platform.

    Two price tiers, kept deliberately distinct (see the two-phase pipeline):

    * ``yes_indicative`` / ``no_indicative`` — the SCREENING price. On Kalshi
      this is the real order-book ask; on Polymarket it is Gamma's mid/last
      quote, which is *not* executable (the two outcome prices sum to exactly
      $1, so each is ~half a spread too cheap).
    * ``yes_ask`` / ``no_ask`` — the CONFIRMED executable best ask. On Kalshi it
      equals the indicative (already a real ask). On Polymarket it is ``None``
      until phase 2 populates it from the CLOB order book. Never let an
      unconfirmed Polymarket number be reported as realizable profit.
    """

    platform: str          # "polymarket" | "kalshi"
    market_id: str         # platform-native id / ticker / slug
    question: str          # primary human-readable question
    description: str       # resolution criteria / rules / subtitle (for the LLM)
    yes_indicative: Optional[float]   # screening price (mid on PM, ask on Kalshi)
    no_indicative: Optional[float]
    yes_ask: Optional[float] = None   # confirmed executable ask (None until phase 2)
    no_ask: Optional[float] = None
    fees_enabled: bool = True         # polymarket: is this market fee-charged?
    clob_tokens: tuple = ()           # polymarket: (yes_token_id, no_token_id)
    category: str = ""     # canonical section (see arb/categories.py)
    event: str = ""        # parent event title (groups related markets)
    close_time: str = ""
    volume: float = 0.0
    liquidity: float = 0.0  # resting order-book dollars (platform-reported)
    url: str = ""
    gamma_id: str = ""     # polymarket-only: numeric Gamma id for batch polling

    def tradeable(self) -> bool:
        """Screenable: has a usable two-sided indicative quote."""
        return (
            self.yes_indicative is not None
            and self.no_indicative is not None
            and 0.0 < self.yes_indicative < 1.0
            and 0.0 < self.no_indicative < 1.0
        )

    @property
    def confirmed(self) -> bool:
        """True once executable asks are known for both sides."""
        return self.yes_ask is not None and self.no_ask is not None

    def buy_yes(self, haircut: float) -> tuple[Optional[float], bool]:
        """(price, confirmed) to BUY the YES side. Falls back to
        indicative + haircut (Polymarket only) when no confirmed ask exists."""
        return self._buy(self.yes_ask, self.yes_indicative, haircut)

    def buy_no(self, haircut: float) -> tuple[Optional[float], bool]:
        return self._buy(self.no_ask, self.no_indicative, haircut)

    def _buy(self, ask, indicative, haircut) -> tuple[Optional[float], bool]:
        if ask is not None:
            return ask, True
        if indicative is None:
            return None, False
        # Polymarket mid understates the ask by ~half a spread; add a haircut.
        bump = haircut if self.platform == "polymarket" else 0.0
        return indicative + bump, False

    def text_blob(self) -> str:
        return f"{self.question} {self.description}".strip()


@dataclass
class CandidateMatch:
    """A heuristically-matched pair of markets, one per platform."""

    polymarket: Market
    kalshi: Market
    similarity: float

    def key(self) -> str:
        return f"{self.polymarket.market_id}::{self.kalshi.market_id}"


@dataclass
class ArbLeg:
    platform: str
    side: str          # "YES" | "NO"
    price: float       # ask paid, dollars (per contract)
    fee: float         # per-contract fee, dollars
    confirmed: bool = True   # False => price is an indicative mid + haircut


@dataclass
class ArbOpportunity:
    """A concrete two-leg arbitrage computed at current prices, net of fees."""

    match: CandidateMatch
    legs: list[ArbLeg]
    cost: float            # total cost per $1 guaranteed payoff (per contract)
    profit: float          # 1 - cost (per contract pair), net of fees
    roi: float             # profit / cost
    contracts: int = 1     # order size the per-contract economics assume
    confirmed: bool = True  # both legs priced from executable asks
    kalshi_fee_is_bound: bool = False  # size unknown => Kalshi fee is upper bound

    # Filled in by the LLM validator.
    validation: Optional["Validation"] = None
    # Filled in by arb.sizing.max_position (a SizingResult) for realized arbs.
    sizing: Optional[Any] = None

    @property
    def realized(self) -> bool:
        """A reportable arbitrage: confirmed prices AND positive net profit."""
        return self.confirmed and self.profit > 0


@dataclass
class Validation:
    """LLM judgment on RESOLUTION CRITERIA only — never on the fee arithmetic.

    Whether an arbitrage actually exists is decided by the (independent) fee
    math plus price confirmation, not by the model.
    """

    same_event: bool
    equivalent_payoff: bool
    confidence: float
    reasoning: str
    caveats: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        """The markets are the same hedgeable event. Says nothing about price."""
        return self.same_event and self.equivalent_payoff
