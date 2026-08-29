"""Exhaustive cross-platform coverage check for one category ("max" mode).

Within a selected canonical section:
  1. Fetch EVERYTHING from both platforms (no per-section caps).
  2. Drop markets below --min-volume (default $10k): a market with no volume can
     never be an executable arb leg, and on a big section the untradeable dust is
     the overwhelming majority of the O(N^2) comparison cost (politics is ~half
     $0-volume markets). Lower the floor toward $1 for wider, slower coverage.
  3. Count distinct events per platform; the platform with FEWER events is the
     "small" side — every one of its events gets checked.
  4. For every event on the small side, heuristically find the best counterpart
     market on the large side (same matcher + guards as the other pipelines).
     Comparison is via matcher.token_index/candidates + similarity_ge, so it only
     scores markets that share a token and skips the O(n^2) ratio() on pairs a
     cheap upper bound already rules out — near-linear instead of all-pairs.
  5. LLM-verify every matched pair with Claude Sonnet 4.6 (low effort):
     same event + equivalent payoff, and whether an arb exists at current
     prices. Uncapped by default — this is the comprehensive mode — so the
     event count is printed up front as a cost signal; --max-validations N
     caps it if needed.

Run:  python -m arb.max --section sports
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict

from dotenv import load_dotenv

from . import arbitrage, categories, matcher, sources
from .fees import FeeConfig
from .models import ArbOpportunity, CandidateMatch, Market


def _eprint(*a):
    print(*a, file=sys.stderr, flush=True)


def fetch_everything(section: str) -> tuple[list[Market], list[Market]]:
    """Exhaustively fetch one section from both platforms."""
    _eprint(f"Fetching ALL '{section}' markets from both platforms "
            "(exhaustive — this can take a while)...")
    pm = sources.fetch_polymarket(per_section=10**6, sections=[section])
    ks = sources.fetch_kalshi(
        per_section=10**6, sections=[section], max_pages=80,
        include_games=(section == "sports"),
    )
    return pm, ks


def group_by_event(markets: list[Market]) -> dict[str, list[Market]]:
    out: dict[str, list[Market]] = {}
    for m in markets:
        if m.tradeable() and m.question:
            out.setdefault(m.event or m.question, []).append(m)
    return out


def match_events(
    small: dict[str, list[Market]],
    large_markets: list[Market],
    threshold: float,
) -> tuple[dict[str, CandidateMatch], list[str]]:
    """Best counterpart pair for every small-side event; also the unmatched.

    Comparison is restricted to large-side markets that share >=1 token with the
    small market (matcher.token_index / matcher.candidates). This is exact — any
    skipped pair shares no token and so scores 0.0 in similarity() — but turns
    the old all-pairs scan (O(small x large): ~260M calls on politics) into a
    near-linear one, which is what let the exhaustive politics run blow past 70
    minutes without ever reaching the LLM step.
    """
    matched: dict[str, CandidateMatch] = {}
    unmatched: list[str] = []
    index = matcher.token_index(large_markets)
    for ev_title, ev_markets in small.items():
        best: CandidateMatch | None = None
        for sm in ev_markets:
            for lg in matcher.candidates(index, sm):
                score = matcher.similarity_ge(sm, lg, threshold)
                if score < threshold:
                    continue
                if best is None or score > best.similarity:
                    if sm.platform == "polymarket":
                        best = CandidateMatch(polymarket=sm, kalshi=lg,
                                              similarity=score)
                    else:
                        best = CandidateMatch(polymarket=lg, kalshi=sm,
                                              similarity=score)
        if best is not None:
            matched[ev_title] = best
        else:
            unmatched.append(ev_title)
    return matched, unmatched


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    p = argparse.ArgumentParser(
        prog="arb.max",
        description="Exhaustive small-platform-vs-large-platform coverage "
                    "check for one category.",
    )
    p.add_argument("--section", required=True, choices=categories.CANONICAL,
                   help="canonical section to check comprehensively")
    p.add_argument("--similarity", type=float, default=0.4,
                   help="heuristic match threshold (default 0.4 — lower than "
                        "the batch scanner since the LLM checks everything)")
    p.add_argument("--min-volume", type=float, default=10000.0,
                   help="drop markets with volume <= this many dollars before "
                        "matching (default 10000). Zero/near-zero-volume markets "
                        "can't be an executable arb yet dominate the O(N^2) "
                        "comparison cost on big sections (e.g. politics is ~half "
                        "$0-volume dust). Clamped to a floor of $1 so dust is "
                        "always excluded; lower it toward $1 for wider, slower "
                        "coverage.")
    p.add_argument("--max-validations", type=int, default=0,
                   help="cap LLM calls (default 0 = validate every match)")
    p.add_argument("--size", type=int, default=1,
                   help="order size (contracts) for per-contract economics")
    p.add_argument("--pm-haircut", type=float, default=0.02,
                   help="spread haircut on Polymarket mids at screen stage")
    p.add_argument("--no-confirm", action="store_true",
                   help="skip CLOB best-ask confirmation (report stays "
                        "indicative and is labeled UNCONFIRMED)")
    p.add_argument("--impact-buffer", type=float, default=0.75,
                   help="cap sizing at this fraction of thinner-leg depth")
    p.add_argument("--gas-cost", type=float, default=0.0,
                   help="fixed $ per round-trip (Polygon gas), amortized")
    p.add_argument("--withdrawal-cost", type=float, default=0.0,
                   help="fixed $ per round-trip (USDC withdrawal), amortized")
    p.add_argument("--no-sizing", action="store_true",
                   help="skip order-book position sizing on confirmed arbs")
    p.add_argument("--kalshi-fee", type=float, default=0.07)
    p.add_argument("--polymarket-fee", type=float, default=None,
                   help="override every Polymarket category with this flat rate")
    p.add_argument("--no-llm", action="store_true",
                   help="heuristic matching only; skip Claude verification")
    p.add_argument("--json", action="store_true",
                   help="emit JSON instead of human-readable output")
    args = p.parse_args(argv)

    cfg = FeeConfig(kalshi_rate=args.kalshi_fee,
                    polymarket_rate_override=args.polymarket_fee)
    contracts = max(1, args.size)

    pm, ks = fetch_everything(args.section)

    # Drop untradeable-dust markets before matching. Volume is the strongest cheap
    # signal here: a $0-volume market can never be an executable arb leg, and on a
    # big section the dust is the overwhelming majority of the O(N^2) comparison
    # cost. Floor at $1 so the $0 dust is always excluded even if --min-volume 0.
    min_volume = max(1.0, args.min_volume)
    n_pm_raw, n_ks_raw = len(pm), len(ks)
    pm = [m for m in pm if m.volume > min_volume]
    ks = [m for m in ks if m.volume > min_volume]
    _eprint(f"Volume filter > ${min_volume:,.0f}: kept polymarket "
            f"{len(pm)}/{n_pm_raw}, kalshi {len(ks)}/{n_ks_raw} "
            f"(dropped {(n_pm_raw - len(pm)) + (n_ks_raw - len(ks))} dust markets).")

    pm_events = group_by_event(pm)
    ks_events = group_by_event(ks)
    _eprint(f"  polymarket: {len(pm_events)} events ({len(pm)} markets)   "
            f"kalshi: {len(ks_events)} events ({len(ks)} markets)")

    if len(pm_events) <= len(ks_events):
        small_name, small = "polymarket", pm_events
        large_markets = [m for m in ks if m.tradeable() and m.question]
    else:
        small_name, small = "kalshi", ks_events
        large_markets = [m for m in pm if m.tradeable() and m.question]
    large_name = "kalshi" if small_name == "polymarket" else "polymarket"
    _eprint(f"Smaller side: {small_name} ({len(small)} events) — checking "
            f"every one against {large_name}...")

    matched, unmatched = match_events(small, large_markets, args.similarity)
    _eprint(f"  {len(matched)} events matched, {len(unmatched)} without a "
            f"counterpart above similarity {args.similarity}")

    # Phase 2: confirm executable CLOB asks on every matched pair's Polymarket
    # leg before pricing (comprehensive mode — confirm all matches).
    if not args.no_confirm and matched:
        pm_legs = {pair.polymarket.market_id: pair.polymarket
                   for pair in matched.values()}
        _eprint(f"Confirming CLOB best-ask for {len(pm_legs)} Polymarket "
                "markets...")
        n_conf = sources.confirm_polymarket_asks(list(pm_legs.values()))
        _eprint(f"  {n_conf} confirmed executable.")

    # Price every matched pair.
    opps: dict[str, ArbOpportunity] = {}
    for ev_title, pair in matched.items():
        opp = arbitrage.best_opportunity(pair, cfg, contracts, args.pm_haircut)
        if opp is not None:
            opps[ev_title] = opp

    if not args.no_llm and opps:
        import anthropic
        from . import validator

        client = anthropic.Anthropic()
        todo = sorted(opps.items(), key=lambda kv: kv[1].profit, reverse=True)
        if args.max_validations > 0:
            todo = todo[: args.max_validations]
        _eprint(f"Verifying {len(todo)} matched pairs with {validator.MODEL} "
                "(low effort, one call each)...")
        for i, (ev_title, opp) in enumerate(todo, 1):
            try:
                opp.validation = validator.validate(opp, client=client)
                if opp.validation.passed:
                    tag = ("ARB" if opp.realized
                           else "equivalent (unconfirmed)" if not opp.confirmed
                           else "equivalent (no edge)")
                else:
                    tag = "different"
            except Exception as e:
                tag = f"error: {e}"
            _eprint(f"  [{i}/{len(todo)}] {tag}: {ev_title[:56]}")

    # Position-size the confirmed, same-event arbs from the order books. Requires
    # a passed validation as well as an executable edge: --no-llm reports raw
    # priced candidates, never confirmed arbs (the same-event check is what makes
    # `profit = 1 - cost` risk-free), so nothing is sized without it.
    to_size = [o for o in opps.values() if o.realized
               and o.validation is not None and o.validation.passed]
    if not args.no_sizing and to_size:
        from . import sizing as sizing_mod
        scfg = sizing_mod.SizingConfig(impact_buffer=args.impact_buffer,
                                       gas_cost=args.gas_cost,
                                       withdrawal_cost=args.withdrawal_cost)
        _eprint(f"Sizing {len(to_size)} confirmed arb(s) from the order books...")
        for o in to_size:
            try:
                o.sizing = sizing_mod.max_position(o, cfg, scfg)
            except Exception as e:
                _eprint(f"  sizing error: {e}")

    if args.json:
        out = []
        for ev_title, pair in matched.items():
            opp = opps.get(ev_title)
            rec = {
                "event": ev_title,
                "small_platform": small_name,
                "similarity": round(pair.similarity, 3),
                "polymarket": asdict(pair.polymarket),
                "kalshi": asdict(pair.kalshi),
            }
            if opp:
                rec["cost"] = round(opp.cost, 4)
                rec["profit"] = round(opp.profit, 4)
                rec["roi"] = round(opp.roi, 4)
                rec["confirmed"] = opp.confirmed
                # The two legs carry the actionable side-to-buy per platform
                # (YES/NO, price, fee); without them the UI can't say what to buy.
                rec["legs"] = [asdict(leg) for leg in opp.legs]
                rec["kalshi_fee_is_bound"] = opp.kalshi_fee_is_bound
                rec["profit_kind"] = ("net_profit" if opp.confirmed
                                      else "indicative_edge_unconfirmed")
                if opp.validation:
                    rec["validation"] = asdict(opp.validation)
                if opp.sizing:
                    rec["sizing"] = asdict(opp.sizing)
            out.append(rec)
        print(json.dumps({"matched": out, "unmatched": unmatched}, indent=2))
        return 0

    # Human-readable report.
    equivalent = [t for t, o in opps.items()
                  if o.validation and o.validation.passed]
    arbs = [t for t in equivalent if opps[t].realized]
    print(f"\n=== MAX coverage report: section '{args.section}' ===")
    print(f"{small_name} (smaller): {len(small)} events | "
          f"{large_name}: markets {len(large_markets)}")
    print(f"matched {len(matched)} | unmatched {len(unmatched)}"
          + (f" | LLM-equivalent {len(equivalent)} | confirmed ARBS {len(arbs)}"
             if not args.no_llm else ""))

    for ev_title, pair in sorted(matched.items(),
                                 key=lambda kv: -(opps[kv[0]].profit
                                                  if kv[0] in opps else -1)):
        opp = opps.get(ev_title)
        v = opp.validation if opp else None
        if v and v.passed:
            if opp.realized:
                badge = " [ARB ✅]"
            elif not opp.confirmed:
                badge = " [equivalent · UNCONFIRMED]"
            else:
                badge = " [equivalent · no edge]"
        elif v:
            badge = " [different ❌]"
        else:
            badge = ""
        edge = ""
        if opp:
            label = "profit" if opp.confirmed else "indicative"
            edge = f"  {label} {opp.profit:+.3f}"
        print("-" * 78)
        print(f"{ev_title[:70]}   sim {pair.similarity:.2f}{edge}{badge}")
        print(f"  PM: {pair.polymarket.question[:72]}")
        print(f"  KS: {pair.kalshi.question[:72]}")
        if v and v.reasoning:
            print(f"  LLM: {v.reasoning[:150]}")
        if opp and opp.sizing:
            s = opp.sizing
            print(f"  SIZE: max ~{s.recommended_size:,.0f} pairs "
                  f"(edge {s.edge_exhaustion_size:,.0f}, depth "
                  f"{s.depth_ceiling_size:,.0f}) -> "
                  f"${s.profit_at_recommended:,.2f} profit")

    if unmatched:
        print("-" * 78)
        print(f"No counterpart found ({len(unmatched)}):")
        for t in unmatched:
            print(f"  - {t[:74]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
