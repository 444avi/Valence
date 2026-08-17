"""Live-game arbitrage monitor.

Flow:
  1. SCAN     — find sports games that are LIVE right now (Polymarket game
                markets carry `gameStartTime`; live = started but not closed).
  2. MATCH    — heuristically pair each live game's markets with Kalshi
                counterparts (same matcher/guards as the batch pipeline).
  3. VALIDATE — one Claude Sonnet 4.6 (low effort) check per pair to confirm
                the resolution criteria line up. Done ONCE, before monitoring —
                never inside the price loop.
  4. MONITOR  — poll ONLY the matched markets, batched: one Kalshi request
                (?tickers=a,b,c) + one Polymarket request (?id=..&id=..) per
                tick. Print an update whenever any tracked price moves >= 1¢,
                and flag ARBITRAGE when the fee-adjusted cost drops below $1.

Run:  python -m arb.live [--interval 5] [--no-llm] [--include-upcoming 30]
Stop with Ctrl+C.
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv

from . import arbitrage, categories, matcher, sources
from .fees import FeeConfig
from .models import ArbOpportunity, CandidateMatch, Market

_TIMEOUT = 15


def _eprint(*a):
    print(*a, file=sys.stderr, flush=True)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


# ---------------------------------------------------------------- 1. SCAN

# A live pair should be about the OUTCOME of the game (who wins / draw / who
# advances), not props. Anything matching these is excluded from monitoring
# unless --all-markets is passed. Kalshi's side is already outcome-only (the
# game-winner series), so this filters the Polymarket side to match.
_PROP_RE = re.compile(
    r"o/u|\bover\b|\bunder\b|spread|both teams|btts|corner|half|exact"
    r"|announcer|first team to score|score or assist|player|total|mention"
    r"|\baces\b|\bhits\b|home run|strikeout|assist|goalscorer|\bgoals\b"
    r"|\bpoints\b|method of|win totals|\bmaps\b|\brounds\b",
    re.IGNORECASE,
)


def is_outcome_market(question: str) -> bool:
    """True if the market is about the game's outcome rather than a prop."""
    return not _PROP_RE.search(question)


def scan_live_polymarket(
    include_upcoming_min: int = 0,
    tag_slugs: list[str] | None = None,
    outcome_only: bool = True,
) -> dict[str, list[dict]]:
    """Return {event_title: [raw market rows]} for games live now (or starting
    within `include_upcoming_min` minutes). Uses gameStartTime on PM markets.

    `tag_slugs` narrows the query server-side (e.g. ["tennis"]); defaults to
    the whole sports section. `outcome_only` drops prop markets, keeping only
    winner / draw / advance style markets.
    """
    now = _now()
    horizon = now + timedelta(minutes=include_upcoming_min)
    live: dict[str, list[dict]] = {}
    seen_ids: set[str] = set()
    for slug in tag_slugs or ["sports"]:
        offset = 0
        while offset < 1000:
            resp = requests.get(
                f"{sources.POLYMARKET_GAMMA_BASE}/events",
                params={
                    "active": "true", "closed": "false", "limit": 100,
                    "offset": offset, "tag_slug": slug,
                    "order": "volume", "ascending": "false",
                },
                timeout=_TIMEOUT * 2,
            )
            resp.raise_for_status()
            events = resp.json()
            if not events:
                break
            for ev in events:
                title = (ev.get("title") or "").strip()
                for m in ev.get("markets", []):
                    start = _parse_ts(m.get("gameStartTime") or "")
                    if start is None or start > horizon:
                        continue
                    end = _parse_ts(m.get("endDate") or "")
                    if end is not None and end < now:
                        continue
                    if m.get("closed"):
                        continue
                    if outcome_only and not is_outcome_market(
                        m.get("question") or ""
                    ):
                        continue
                    mid = str(m.get("id", ""))
                    if mid in seen_ids:
                        continue
                    seen_ids.add(mid)
                    live.setdefault(title, []).append(m)
            offset += 100
            if len(events) < 100:
                break
    return live


# --------------------------------------------------------------- 2. MATCH

def build_matches(
    live_events: dict[str, list[dict]],
    threshold: float,
    series: list[str] | None = None,
) -> list[CandidateMatch]:
    pm_markets: list[Market] = []
    for title, rows in live_events.items():
        for row in rows:
            m = sources._parse_polymarket_row(row, "sports", title)
            if m is not None and m.tradeable():
                pm_markets.append(m)
    ks_markets = sources._fetch_kalshi_game_series(
        series or categories.KALSHI_GAME_SERIES, 2000
    )
    _eprint(f"  live PM markets: {len(pm_markets)}   Kalshi game markets: {len(ks_markets)}")
    return matcher.find_matches(pm_markets, ks_markets, threshold=threshold)


# ------------------------------------------------------------- 4. MONITOR

def _poll_kalshi(tickers: list[str]) -> dict[str, tuple[float, float]]:
    """One batched request -> {ticker: (yes_ask, no_ask)} in dollars."""
    out: dict[str, tuple[float, float]] = {}
    for i in range(0, len(tickers), 40):  # stay well under URL length limits
        chunk = tickers[i:i + 40]
        resp = requests.get(
            f"{sources.KALSHI_API_BASE}/markets",
            params={"tickers": ",".join(chunk)},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        for m in resp.json().get("markets", []):
            try:
                out[m["ticker"]] = (
                    float(m["yes_ask_dollars"]), float(m["no_ask_dollars"])
                )
            except (KeyError, TypeError, ValueError):
                continue
    return out


def _poll_polymarket(ids: list[str]) -> dict[str, tuple[float, float]]:
    """One batched request -> {gamma_id: (yes_mid, no_mid)} — INDICATIVE mids.

    Live polling uses Gamma mids (CLOB confirmation every tick would be far too
    many requests), so opportunities here are flagged unconfirmed downstream.
    """
    out: dict[str, tuple[float, float]] = {}
    for i in range(0, len(ids), 40):
        chunk = ids[i:i + 40]
        resp = requests.get(
            f"{sources.POLYMARKET_GAMMA_BASE}/markets",
            params=[("id", x) for x in chunk],
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        for row in resp.json():
            m = sources._parse_polymarket_row(row, "sports")
            if m is not None and m.yes_indicative is not None \
                    and m.no_indicative is not None:
                out[str(row.get("id"))] = (m.yes_indicative, m.no_indicative)
    return out


def _fmt_opp(opp: ArbOpportunity) -> str:
    legs = " + ".join(
        f"{l.platform[:2].upper()} {l.side} {l.price:.2f}(fee {l.fee:.3f})"
        for l in opp.legs
    )
    # Live prices are Gamma mids + haircut, never CLOB-confirmed, so an edge is
    # only ever a candidate to confirm on the book — never a realized arb.
    tag = "  *** INDICATIVE ARB (confirm on book) ***" if opp.profit > 0 else ""
    return f"{legs} | cost {opp.cost:.3f} edge {opp.profit:+.3f}{tag}"


def monitor(
    pairs: list[CandidateMatch],
    cfg: FeeConfig,
    interval: float,
    min_move: float,
    min_price: float = 0.02,
    contracts: int = 1,
    haircut: float = 0.02,
) -> None:
    pm_ids = {p.polymarket.market_id: p.polymarket.gamma_id for p in pairs}
    ks_tickers = sorted({p.kalshi.market_id for p in pairs})

    last: dict[str, tuple[float, ...]] = {}
    _eprint(
        f"Monitoring {len(pairs)} pair(s) every {interval:.0f}s "
        f"(update on moves >= {min_move:.2f}). Ctrl+C to stop."
    )
    while True:
        try:
            ks_px = _poll_kalshi(ks_tickers)
            pm_px = _poll_polymarket([v for v in pm_ids.values() if v])
        except requests.RequestException as e:
            _eprint(f"[poll error, retrying] {e}")
            time.sleep(interval)
            continue

        ts = _now().strftime("%H:%M:%S")
        for pair in pairs:
            pm, ks = pair.polymarket, pair.kalshi
            pm_q = pm_px.get(pm_ids.get(pm.market_id) or "")
            ks_q = ks_px.get(ks.market_id)
            if pm_q is None or ks_q is None:
                continue
            # PM quotes are indicative mids (ask stays None -> unconfirmed);
            # Kalshi quotes are real asks (indicative == confirmed).
            pm.yes_indicative, pm.no_indicative = pm_q
            ks.yes_indicative, ks.no_indicative = ks_q
            ks.yes_ask, ks.no_ask = ks_q
            if not (pm.tradeable() and ks.tradeable()):
                continue  # settled/one-sided; skip silently
            snap = (pm.yes_indicative, pm.no_indicative,
                    ks.yes_indicative, ks.no_indicative)
            if min(snap) < min_price:
                # Near-settled: one leg is effectively decided and the other
                # book is stale — any "arb" here isn't executable.
                continue
            prev = last.get(pair.key())
            moved = prev is None or any(
                abs(a - b) >= min_move for a, b in zip(snap, prev)
            )
            if not moved:
                continue
            last[pair.key()] = snap
            opp = arbitrage.best_opportunity(pair, cfg, contracts, haircut)
            if opp is None:
                continue
            print(
                f"[{ts}] {ks.question[:46]:46s} "
                f"PM y/n {snap[0]:.2f}/{snap[1]:.2f} "
                f"KS y/n {snap[2]:.2f}/{snap[3]:.2f} | {_fmt_opp(opp)}",
                flush=True,
            )
        time.sleep(interval)


# ----------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    p = argparse.ArgumentParser(
        prog="arb.live", description="Monitor live sports games for arbitrage."
    )
    p.add_argument("--similarity", type=float, default=0.45)
    p.add_argument("--interval", type=float, default=5.0,
                   help="seconds between polls (default 5; 2 requests/poll)")
    p.add_argument("--min-move", type=float, default=0.01,
                   help="min price move to report, dollars (default 0.01)")
    p.add_argument("--min-price", type=float, default=0.02,
                   help="skip pairs with any ask below this (default 0.02): "
                        "near-settled legs against a stale book look like "
                        "arbs but aren't executable")
    p.add_argument("--include-upcoming", type=int, default=0, metavar="MIN",
                   help="also monitor games starting within MIN minutes")
    p.add_argument("--sport", type=str, default=None,
                   help="comma list of sports to scan (much faster): "
                        + ",".join(categories.SPORT_MAP) + "; aliases like "
                        "mlb/nfl/nba/ufc also work. Default: all sports")
    p.add_argument("--all-markets", action="store_true",
                   help="also monitor prop markets (O/U, spreads, BTTS, ...); "
                        "default is outcome markets only (winner/draw/advance)")
    p.add_argument("--min-liquidity", type=float, default=0.0, metavar="USD",
                   help="skip pairs where a leg reports liquidity below this "
                        "(default 0 = off). Empty PM books quote a fake 0.50 "
                        "mid that looks like a huge arb; ~20 filters those")
    p.add_argument("--prioritize", choices=["thin", "sim"], default="thin",
                   help="order for the LLM validation budget: 'thin' = lowest "
                        "combined liquidity first (dislocations are likelier "
                        "in thin books); 'sim' = highest similarity first")
    p.add_argument("--max-validations", type=int, default=10)
    p.add_argument("--size", type=int, default=1,
                   help="order size (contracts) for per-contract economics")
    p.add_argument("--pm-haircut", type=float, default=0.02,
                   help="spread haircut on Polymarket mids (default 0.02/leg); "
                        "live prices are never CLOB-confirmed")
    p.add_argument("--kalshi-fee", type=float, default=0.07)
    p.add_argument("--polymarket-fee", type=float, default=None,
                   help="override every Polymarket category with this flat rate")
    p.add_argument("--no-llm", action="store_true",
                   help="skip Claude resolution-criteria validation")
    args = p.parse_args(argv)

    cfg = FeeConfig(kalshi_rate=args.kalshi_fee,
                    polymarket_rate_override=args.polymarket_fee)

    # Resolve --sport into PM tag slugs + Kalshi series (None = everything).
    pm_tags: list[str] | None = None
    ks_series: list[str] | None = None
    if args.sport:
        pm_tags, ks_series = [], []
        unknown = []
        for raw in args.sport.split(","):
            name = raw.strip().lower()
            name = categories.SPORT_ALIASES.get(name, name)
            if name in categories.SPORT_MAP:
                tags, series = categories.SPORT_MAP[name]
                pm_tags += [t for t in tags if t not in pm_tags]
                ks_series += [s for s in series if s not in ks_series]
            else:
                unknown.append(raw.strip())
        if unknown:
            _eprint(f"Unknown sport(s) {unknown}; valid: "
                    f"{', '.join(categories.SPORT_MAP)} (+aliases "
                    f"{', '.join(categories.SPORT_ALIASES)})")
            return 2
        _eprint(f"Sport filter: PM tags {pm_tags} | Kalshi series {ks_series}")

    _eprint("Scanning for live games (Polymarket gameStartTime)...")
    live_events = scan_live_polymarket(
        args.include_upcoming,
        tag_slugs=pm_tags,
        outcome_only=not args.all_markets,
    )
    n_mk = sum(len(v) for v in live_events.values())
    if not live_events:
        _eprint("No live games found. Try --include-upcoming 60 to catch "
                "games starting within the hour.")
        return 0
    _eprint(f"  live games: {len(live_events)} ({n_mk} markets)")
    for t in live_events:
        _eprint(f"    - {t}")

    # Stash gamma numeric ids on the Market objects for batched polling.
    id_by_slug: dict[str, str] = {}
    for rows in live_events.values():
        for row in rows:
            if row.get("slug"):
                id_by_slug[row["slug"]] = str(row.get("id", ""))

    _eprint("Matching against Kalshi game markets...")
    matches = build_matches(live_events, args.similarity, series=ks_series)
    _eprint(f"  {len(matches)} candidate pairs")
    if args.min_liquidity > 0:
        # Only drop when the platform actually reported a number (0 = unknown).
        def liquid_enough(m: Market) -> bool:
            return m.liquidity == 0 or m.liquidity >= args.min_liquidity

        before = len(matches)
        matches = [m for m in matches
                   if liquid_enough(m.polymarket) and liquid_enough(m.kalshi)]
        _eprint(f"  {before - len(matches)} dropped below "
                f"${args.min_liquidity:,.0f} reported liquidity")
    if not matches:
        return 0
    for m in matches:
        m.polymarket.gamma_id = id_by_slug.get(m.polymarket.market_id, "")

    if args.prioritize == "thin":
        # Thin books reprice slowly and drift apart, so dislocations (and
        # therefore arbs) are likelier there — spend the LLM budget on them
        # first. The thinner leg is the binding one, but a reported 0 means
        # "unknown" (both platforms return 0 when the field is missing), so
        # only min() over known values; fully-unknown pairs go last.
        def pair_liquidity(p: CandidateMatch) -> float:
            known = [v for v in (p.polymarket.liquidity, p.kalshi.liquidity) if v > 0]
            return min(known) if known else float("inf")

        def fmt_liq(v: float) -> str:
            # 0 means the platform didn't report it; sub-$100 books are shown
            # with cents (thin markets often hold under a dollar of resting
            # orders, which ",.0f" would misleadingly render as $0).
            if v == 0:
                return "     n/a"
            return f"${v:>10,.2f}" if v < 100 else f"${v:>10,.0f}"

        matches.sort(key=pair_liquidity)
        _eprint("  validation order (thinnest combined book first):")
        for pair in matches[: args.max_validations]:
            _eprint(
                f"    liq PM {fmt_liq(pair.polymarket.liquidity)} | "
                f"KS {fmt_liq(pair.kalshi.liquidity)} | "
                f"{pair.kalshi.question[:44]}"
            )

    if not args.no_llm:
        import anthropic
        from . import validator

        client = anthropic.Anthropic()
        n = min(len(matches), args.max_validations)
        _eprint(f"Validating resolution criteria for top {n} pairs with "
                f"{validator.MODEL} (low effort, once per pair)...")
        kept: list[CandidateMatch] = []
        for i, pair in enumerate(matches[:n], 1):
            opp = arbitrage.best_opportunity(pair, cfg, args.size, args.pm_haircut)
            if opp is None:
                continue
            try:
                v = validator.validate(opp, client=client)
            except Exception as e:
                _eprint(f"  [{i}/{n}] validation error ({e}); keeping pair unvalidated")
                kept.append(pair)
                continue
            # For monitoring, what matters is that the markets are the same
            # hedgeable event; the price edge is recomputed every tick.
            ok = v.passed
            _eprint(f"  [{i}/{n}] {'KEEP' if ok else 'drop'}: {pair.kalshi.question[:50]}"
                    f"  ({v.reasoning[:80]})")
            if ok:
                kept.append(pair)
        matches = kept

    if not matches:
        _eprint("No validated pairs to monitor.")
        return 0

    try:
        monitor(matches, cfg, interval=args.interval, min_move=args.min_move,
                min_price=args.min_price, contracts=max(1, args.size),
                haircut=args.pm_haircut)
    except KeyboardInterrupt:
        _eprint("\nStopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
