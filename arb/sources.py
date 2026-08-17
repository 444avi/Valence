"""Fetch and normalize markets from Polymarket and Kalshi public APIs.

Neither endpoint used here requires authentication for read access.
"""

from __future__ import annotations

import json
import os
from typing import Any

import requests

POLYMARKET_GAMMA_BASE = os.environ.get(
    "POLYMARKET_GAMMA_BASE", "https://gamma-api.polymarket.com"
)
POLYMARKET_CLOB_BASE = os.environ.get(
    "POLYMARKET_CLOB_BASE", "https://clob.polymarket.com"
)
KALSHI_API_BASE = os.environ.get(
    "KALSHI_API_BASE", "https://api.elections.kalshi.com/trade-api/v2"
)

from . import categories  # noqa: E402
from .models import Market  # noqa: E402

_TIMEOUT = 30


def _to_float(v: Any) -> float | None:
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def fetch_polymarket(
    per_section: int = 120, sections: list[str] | None = None
) -> list[Market]:
    """Fetch active, open Polymarket binary markets, one canonical section at a
    time, from the GLOBAL Polymarket network (gamma-api.polymarket.com — the
    USDC-settled markets, not the US-regulated venue).

    For each canonical section we query the Gamma /events endpoint filtered by
    that section's tag slug(s), ordered by volume, and flatten the nested
    markets. Each market is stamped with its canonical category so matching only
    ever compares like-with-like.

    Prices come from Gamma's `outcomePrices` (mid/last), so they are
    *indicative* rather than guaranteed-executable best-ask. Good enough for
    surfacing candidates; confirm on the order book before trading.
    """
    sections = categories.resolve_sections(sections)
    by_id: dict[str, Market] = {}
    for canon in sections:
        collected = 0
        for slug in categories.POLYMARKET_TAGS.get(canon, []):
            if collected >= per_section:
                break
            offset = 0
            while collected < per_section:
                batch = min(100, per_section - collected)
                params = {
                    "active": "true",
                    "closed": "false",
                    "archived": "false",
                    "limit": batch,
                    "offset": offset,
                    "order": "volume",
                    "ascending": "false",
                    "tag_slug": slug,
                }
                resp = requests.get(
                    f"{POLYMARKET_GAMMA_BASE}/events", params=params, timeout=_TIMEOUT
                )
                if resp.status_code == 422:
                    # Gamma caps pagination depth (~2000); volume-ordered, so
                    # everything past here is dust — treat as end of feed.
                    break
                resp.raise_for_status()
                events = resp.json()
                if not events:
                    break
                for ev in events:
                    if collected >= per_section:
                        break
                    ev_title = (ev.get("title") or "").strip()
                    for row in ev.get("markets", []):
                        m = _parse_polymarket_row(row, canon, ev_title)
                        if m is None or not m.tradeable():
                            continue
                        if m.market_id not in by_id:
                            by_id[m.market_id] = m
                            collected += 1
                offset += len(events)
                if len(events) < batch:
                    break
    return list(by_id.values())


def _parse_polymarket_row(
    row: dict, canon: str = "", ev_title: str = ""
) -> Market | None:
    try:
        outcomes = row.get("outcomes")
        prices = row.get("outcomePrices")
        if isinstance(outcomes, str):
            outcomes = json.loads(outcomes)
        if isinstance(prices, str):
            prices = json.loads(prices)
        if not outcomes or not prices or len(outcomes) != 2:
            return None
        # Identify which index is YES vs NO.
        lower = [str(o).strip().lower() for o in outcomes]
        outcome_label = ""
        if "yes" in lower and "no" in lower:
            yi, ni = lower.index("yes"), lower.index("no")
        else:
            # Two-outcome side market (e.g. "Team to Advance": [USA, BIH]).
            # Polymarket binary outcomes are complementary shares (exactly one
            # pays $1), so treat outcomes[0] as the YES side and fold its label
            # into the question so matching knows which side this is.
            yi, ni = 0, 1
            outcome_label = str(outcomes[0]).strip()
        # These are Gamma mid/last quotes — INDICATIVE, not executable asks.
        yes_mid = _to_float(prices[yi])
        no_mid = _to_float(prices[ni])
        tokens = row.get("clobTokenIds")
        if isinstance(tokens, str):
            tokens = json.loads(tokens)
        clob_tokens = (
            (str(tokens[yi]), str(tokens[ni]))
            if isinstance(tokens, list) and len(tokens) == 2
            else ()
        )
    except (json.JSONDecodeError, ValueError, IndexError, TypeError):
        return None

    slug = row.get("slug", "")
    mq = (row.get("question") or "").strip()
    # Always append the side label: even when the team is already named in the
    # question (e.g. "Team to Advance" names both), repeating the YES side is
    # what lets the matcher's repeated-subject guard identify this market's
    # subject correctly.
    if outcome_label:
        mq = f"{mq} {outcome_label}"
    # Polymarket often titles team-vs-team / dated markets without the opponent
    # (e.g. "Will Argentina win on 2026-06-22?"); the parent EVENT carries the
    # matchup ("Argentina vs. Austria"). Fold the event title in — mirroring how
    # we build Kalshi questions — so the two platforms become comparable.
    if ev_title and ev_title.lower() not in mq.lower():
        question = f"{ev_title} {mq}".strip()
    else:
        question = mq
    fees_enabled = bool(row.get("feesEnabled", False))
    return Market(
        platform="polymarket",
        market_id=slug or str(row.get("id", "")),
        question=question,
        description=(row.get("description") or "").strip(),
        yes_indicative=yes_mid,
        no_indicative=no_mid,
        yes_ask=None,   # populated later by confirm_polymarket_asks (phase 2)
        no_ask=None,
        fees_enabled=fees_enabled,
        clob_tokens=clob_tokens,
        category=categories.polymarket_canonical(canon, question),
        event=ev_title,
        close_time=row.get("endDate", "") or "",
        volume=_to_float(row.get("volumeNum")) or 0.0,
        liquidity=_to_float(row.get("liquidityNum")) or 0.0,
        url=f"https://polymarket.com/market/{slug}" if slug else "",
    )


def _clob_best_ask(token_id: str) -> float | None:
    """Lowest ask (executable buy price) for a CLOB token, or None if no book."""
    try:
        resp = requests.get(
            f"{POLYMARKET_CLOB_BASE}/book",
            params={"token_id": token_id},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        asks = resp.json().get("asks", [])
    except (requests.RequestException, ValueError):
        return None
    prices = [_to_float(a.get("price")) for a in asks]
    prices = [p for p in prices if p is not None and 0.0 < p < 1.0]
    return min(prices) if prices else None


def confirm_polymarket_asks(markets: list[Market]) -> int:
    """Phase 2: populate executable best asks from the CLOB order book.

    Only call this on the handful of markets that survive screening — it makes
    two HTTP requests per market. Sets ``yes_ask``/``no_ask`` in place; a market
    whose book is empty stays unconfirmed (asks remain None). Returns the count
    of markets that became confirmed.
    """
    confirmed = 0
    for m in markets:
        if m.platform != "polymarket" or len(m.clob_tokens) != 2:
            continue
        yes_ask = _clob_best_ask(m.clob_tokens[0])
        no_ask = _clob_best_ask(m.clob_tokens[1])
        if yes_ask is not None:
            m.yes_ask = yes_ask
        if no_ask is not None:
            m.no_ask = no_ask
        if m.confirmed:
            confirmed += 1
    return confirmed


def fetch_kalshi(
    per_section: int = 120,
    sections: list[str] | None = None,
    max_pages: int = 40,
    include_games: bool = True,
    game_limit: int = 600,
) -> list[Market]:
    """Fetch open Kalshi markets via the /events endpoint, bucketed by section.

    The bare /markets feed is currently dominated by multi-game parlay (MVE)
    combos with degenerate 0/1 quotes. The /events endpoint with nested markets
    returns the real, individually-tradeable contracts; each event also carries
    a `category` we map to a canonical section.

    Kalshi's open feed is heavily skewed toward Elections, so we cap each
    canonical section at `per_section` and keep paginating until every requested
    section is full (or we hit `max_pages` / run out of events).

    When `sports` is requested and `include_games` is set, we ALSO pull the
    game-winner series (see KALSHI_GAME_SERIES) directly by series_ticker, since
    those moneyline markets are buried too deep in the general feed to surface.
    """
    sections = categories.resolve_sections(sections)
    counts: dict[str, int] = {s: 0 for s in sections}
    by_id: dict[str, Market] = {}
    cursor = None
    for _ in range(max_pages):
        if all(counts[s] >= per_section for s in sections):
            break
        params: dict[str, Any] = {
            "status": "open",
            "with_nested_markets": "true",
            "limit": 200,
        }
        if cursor:
            params["cursor"] = cursor
        resp = requests.get(
            f"{KALSHI_API_BASE}/events", params=params, timeout=_TIMEOUT
        )
        resp.raise_for_status()
        data = resp.json()
        events = data.get("events", [])
        if not events:
            break
        for ev in events:
            ev_title = (ev.get("title") or "").strip()
            ev_sub = (ev.get("sub_title") or "").strip()
            ev_category = ev.get("category")
            for row in ev.get("markets", []):
                m = _parse_kalshi_market(row, ev_title, ev_sub, ev_category)
                if m is None or not m.tradeable():
                    continue
                if m.category not in counts or counts[m.category] >= per_section:
                    continue
                if m.market_id not in by_id:
                    by_id[m.market_id] = m
                    counts[m.category] += 1
        cursor = data.get("cursor")
        if not cursor:
            break

    if include_games and "sports" in sections:
        for m in _fetch_kalshi_game_series(categories.KALSHI_GAME_SERIES, game_limit):
            by_id.setdefault(m.market_id, m)

    return list(by_id.values())


def _fetch_kalshi_game_series(series: list[str], total_limit: int) -> list[Market]:
    """Fetch winner markets for specific game/match series directly by ticker."""
    out: list[Market] = []
    for series_ticker in series:
        if len(out) >= total_limit:
            break
        cursor = None
        for _ in range(10):  # generous page cap per series
            params: dict[str, Any] = {
                "series_ticker": series_ticker,
                "status": "open",
                "with_nested_markets": "true",
                "limit": 200,
            }
            if cursor:
                params["cursor"] = cursor
            try:
                resp = requests.get(
                    f"{KALSHI_API_BASE}/events", params=params, timeout=_TIMEOUT
                )
                resp.raise_for_status()
            except requests.RequestException:
                break
            data = resp.json()
            events = data.get("events", [])
            if not events:
                break
            for ev in events:
                ev_title = (ev.get("title") or "").strip()
                ev_sub = (ev.get("sub_title") or "").strip()
                # These series are all sports; default the category if absent.
                ev_category = ev.get("category") or "Sports"
                for row in ev.get("markets", []):
                    m = _parse_kalshi_market(row, ev_title, ev_sub, ev_category)
                    if m is None or not m.tradeable():
                        continue
                    m.category = "sports"  # esports series map here too
                    out.append(m)
            cursor = data.get("cursor")
            if not cursor:
                break
    return out


def _parse_kalshi_market(
    row: dict, ev_title: str, ev_sub: str, ev_category: str | None = None
) -> Market | None:
    # Skip multi-variate parlay legs — not single-event binaries.
    if row.get("mve_collection_ticker") or row.get("ticker", "").startswith("KXMVE"):
        return None

    # Prices come as dollar strings via `*_dollars`; fall back to legacy cents.
    yes_ask = _to_float(row.get("yes_ask_dollars"))
    no_ask = _to_float(row.get("no_ask_dollars"))
    if yes_ask is None and (cents := _to_float(row.get("yes_ask"))) is not None:
        yes_ask = cents / 100.0
    if no_ask is None and (cents := _to_float(row.get("no_ask"))) is not None:
        no_ask = cents / 100.0

    # Compose a specific question: event title + the market's outcome label.
    outcome = (
        row.get("yes_sub_title")
        or row.get("subtitle")
        or row.get("title")
        or ""
    ).strip()
    if outcome and outcome.lower() != ev_title.lower():
        question = f"{ev_title} {outcome}".strip()
    else:
        question = ev_title

    rules = (row.get("rules_primary") or ev_sub or "").strip()
    ticker = row.get("ticker", "")
    volume = (
        _to_float(row.get("volume_fp"))
        or _to_float(row.get("volume"))
        or _to_float(row.get("volume_24h_fp"))
        or 0.0
    )

    # Kalshi order-book asks are already executable, so indicative == confirmed.
    return Market(
        platform="kalshi",
        market_id=ticker,
        question=question,
        description=rules,
        yes_indicative=yes_ask,
        no_indicative=no_ask,
        yes_ask=yes_ask,
        no_ask=no_ask,
        category=categories.kalshi_canonical(ev_category, question),
        event=ev_title,
        close_time=row.get("close_time", "") or "",
        volume=volume,
        # Kalshi's liquidity_dollars is unpopulated (always 0) as of mid-2026;
        # fall back to open interest (contracts, each <=$1) as a thinness proxy.
        liquidity=(
            _to_float(row.get("liquidity_dollars"))
            or _to_float(row.get("open_interest_fp"))
            or 0.0
        ),
        url=f"https://kalshi.com/markets/{ticker}" if ticker else "",
    )
