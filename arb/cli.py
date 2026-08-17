"""Command-line entry point: fetch -> match -> screen -> confirm -> validate.

Two-phase pricing (see arb/models.Market):
  phase 1 (screen)  Gamma mids + spread haircut — cheap, wide, INDICATIVE.
  phase 2 (confirm) CLOB best-ask on the survivors only, before LLM spend.
No number is reported as realizable "profit" unless phase 2 confirmed it.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict

from dotenv import load_dotenv

from . import arbitrage, categories, matcher, sources
from .fees import FeeConfig
from .models import ArbOpportunity


def _eprint(*a):
    print(*a, file=sys.stderr, flush=True)


def _opp_to_dict(opp: ArbOpportunity) -> dict:
    d = {
        "polymarket": asdict(opp.match.polymarket),
        "kalshi": asdict(opp.match.kalshi),
        "similarity": round(opp.match.similarity, 3),
        "legs": [asdict(leg) for leg in opp.legs],
        "cost": round(opp.cost, 4),
        "profit": round(opp.profit, 4),
        "roi": round(opp.roi, 4),
        "confirmed": opp.confirmed,
        "contracts": opp.contracts,
        "kalshi_fee_is_bound": opp.kalshi_fee_is_bound,
        "profit_kind": ("net_profit" if opp.confirmed
                        else "indicative_edge_unconfirmed"),
    }
    if opp.validation is not None:
        d["validation"] = asdict(opp.validation)
    if opp.sizing is not None:
        d["sizing"] = asdict(opp.sizing)
    return d


def _badge(opp: ArbOpportunity) -> str:
    v = opp.validation
    if v is None:
        return ""
    if not v.passed:
        return "  [different ❌]"
    if not opp.confirmed:
        return "  [same event · price UNCONFIRMED]"
    if opp.profit > 0:
        return "  [ARBITRAGE ✅]"
    return "  [same event · no edge]"


def _print_human(opp: ArbOpportunity) -> None:
    m = opp.match
    v = opp.validation
    kind = "net profit" if opp.confirmed else "INDICATIVE edge (unconfirmed)"
    print("=" * 78)
    print(f"[{m.kalshi.category}]  {kind} ${opp.profit:+.3f}/contract  "
          f"ROI {opp.roi * 100:+.1f}%  sim {m.similarity:.2f}{_badge(opp)}")
    print(f"  PM: {m.polymarket.question}")
    print(f"      {m.polymarket.url}")
    print(f"  KS: {m.kalshi.question}")
    print(f"      {m.kalshi.url}")
    legs = "  +  ".join(
        f"{l.platform} {l.side} ${l.price:.3f}"
        f"{'' if l.confirmed else '~'} (fee ${l.fee:.4f})"
        for l in opp.legs
    )
    print(f"  legs: {legs}   total ${opp.cost:.3f}")
    notes = []
    if not opp.confirmed:
        notes.append("~ = Gamma mid + haircut, NOT an executable ask")
    if opp.kalshi_fee_is_bound:
        notes.append("Kalshi fee is an UPPER BOUND at size 1 (use --size)")
    if notes:
        print("  note: " + "; ".join(notes))
    if v is not None:
        print(f"  LLM: same_event={v.same_event} "
              f"equiv_payoff={v.equivalent_payoff} conf={v.confidence:.2f}")
        print(f"       {v.reasoning}")
        for c in v.caveats:
            print(f"       - caveat: {c}")
    _print_sizing(opp)


def _print_sizing(opp: ArbOpportunity) -> None:
    s = opp.sizing
    if s is None:
        return
    size = s.recommended_size
    print(f"  SIZE: max ~{size:,.0f} pairs  (edge-exhaustion "
          f"{s.edge_exhaustion_size:,.0f}, depth ceiling {s.depth_ceiling_size:,.0f}, "
          f"book cap {s.max_fillable:,.0f})")
    if size > 0:
        stake = (s.yes_leg.avg_price + s.no_leg.avg_price) * size
        print(f"        ~${stake:,.0f} deployed -> ${s.profit_at_recommended:,.2f} "
              f"profit (spread {s.spread_at_recommended:+.3f}/pair at size); "
              f"avg fills YES {s.yes_leg.avg_price:.3f} / NO {s.no_leg.avg_price:.3f}")
    for n in s.notes:
        print(f"        - {n}")


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    p = argparse.ArgumentParser(
        prog="arb",
        description="Find arbitrage between Polymarket and Kalshi.",
    )
    p.add_argument("--limit", type=int, default=720,
                   help="max markets per platform, split across sections (default 720); "
                        "fewer --sections => deeper coverage each")
    p.add_argument("--per-section", type=int, default=None,
                   help="max markets per section per platform (overrides --limit)")
    p.add_argument("--sections", type=str, default=None,
                   help="comma list of sections to scan: "
                        + ",".join(categories.CANONICAL) + " (default all)")
    p.add_argument("--similarity", type=float, default=0.45,
                   help="heuristic match threshold 0-1 (default 0.45)")
    p.add_argument("--min-profit", type=float, default=0.0,
                   help="min net profit per contract to report, dollars (default 0)")
    p.add_argument("--size", type=int, default=1,
                   help="order size (contracts) for per-contract economics "
                        "(default 1; at 1 the Kalshi fee is an upper bound)")
    p.add_argument("--pm-haircut", type=float, default=0.02,
                   help="spread haircut added to Polymarket mids at the screen "
                        "stage before CLOB confirmation (default 0.02/leg)")
    p.add_argument("--no-confirm", action="store_true",
                   help="skip CLOB best-ask confirmation (report stays "
                        "indicative and is labeled UNCONFIRMED)")
    p.add_argument("--max-validations", type=int, default=15,
                   help="cap LLM validation calls (default 15)")
    p.add_argument("--impact-buffer", type=float, default=0.75,
                   help="cap sizing at this fraction of thinner-leg depth "
                        "(default 0.75; 1.0 = use full visible depth)")
    p.add_argument("--gas-cost", type=float, default=0.0,
                   help="fixed $ cost per round-trip (Polygon gas), amortized")
    p.add_argument("--withdrawal-cost", type=float, default=0.0,
                   help="fixed $ cost per round-trip (USDC withdrawal), amortized")
    p.add_argument("--no-sizing", action="store_true",
                   help="skip order-book position sizing on confirmed arbs")
    p.add_argument("--kalshi-fee", type=float, default=0.07,
                   help="Kalshi fee rate (default 0.07)")
    p.add_argument("--polymarket-fee", type=float, default=None,
                   help="override EVERY Polymarket category with this flat rate "
                        "(default: per-category table)")
    p.add_argument("--no-games", action="store_true",
                   help="skip targeted Kalshi sports game-winner series coverage")
    p.add_argument("--no-llm", action="store_true",
                   help="skip Claude validation; report raw priced candidates")
    p.add_argument("--json", action="store_true",
                   help="emit JSON instead of human-readable output")
    args = p.parse_args(argv)

    cfg = FeeConfig(kalshi_rate=args.kalshi_fee,
                    polymarket_rate_override=args.polymarket_fee)
    contracts = max(1, args.size)

    sections = categories.resolve_sections(
        args.sections.split(",") if args.sections else None
    )
    per_section = args.per_section or max(20, args.limit // len(sections))

    _eprint(f"Sections: {', '.join(sections)}")
    _eprint(f"Fetching up to {per_section} markets/section per platform...")
    pm = sources.fetch_polymarket(per_section=per_section, sections=sections)
    ks = sources.fetch_kalshi(per_section=per_section, sections=sections,
                              include_games=not args.no_games)
    _eprint(f"  polymarket: {len(pm)} markets   kalshi: {len(ks)} markets")

    _eprint("Heuristic matching...")
    matches = matcher.find_matches(pm, ks, threshold=args.similarity)
    _eprint(f"  {len(matches)} candidate pairs above similarity {args.similarity}")

    # Phase 1: screen on indicative mids + haircut.
    opps = arbitrage.find_opportunities(
        matches, cfg, min_profit=args.min_profit,
        contracts=contracts, haircut=args.pm_haircut,
    )
    _eprint(f"  {len(opps)} screened candidates (indicative) with edge "
            f"> ${args.min_profit}")

    # Phase 2: confirm executable asks on the survivors, then re-price.
    if not args.no_confirm and opps:
        confirm_budget = max(args.max_validations, 30)
        pm_to_confirm = {o.match.polymarket.market_id: o.match.polymarket
                         for o in opps[:confirm_budget]}
        _eprint(f"Confirming CLOB best-ask for {len(pm_to_confirm)} Polymarket "
                "markets...")
        n_conf = sources.confirm_polymarket_asks(list(pm_to_confirm.values()))
        _eprint(f"  {n_conf} confirmed executable; re-pricing...")
        opps = arbitrage.find_opportunities(
            [o.match for o in opps], cfg, min_profit=args.min_profit,
            contracts=contracts, haircut=args.pm_haircut,
        )

    if not args.no_llm and opps:
        import anthropic
        from . import validator

        client = anthropic.Anthropic()
        n = min(len(opps), args.max_validations)
        _eprint(f"Validating top {n} with {validator.MODEL} (low effort)...")
        for i, opp in enumerate(opps[:n], 1):
            try:
                opp.validation = validator.validate(opp, client=client)
                _eprint(f"  [{i}/{n}] {'same-event' if opp.validation.passed else 'different'}")
            except Exception as e:  # keep going; report what we can
                _eprint(f"  [{i}/{n}] validation error: {e}")

    # Position-size the confirmed, same-event arbs (walks both order books).
    realized = [o for o in opps if o.realized
                and (args.no_llm or (o.validation and o.validation.passed))]
    if not args.no_sizing and realized:
        from . import sizing as sizing_mod
        scfg = sizing_mod.SizingConfig(impact_buffer=args.impact_buffer,
                                       gas_cost=args.gas_cost,
                                       withdrawal_cost=args.withdrawal_cost)
        _eprint(f"Sizing {len(realized)} confirmed arb(s) from the order books...")
        for o in realized:
            try:
                o.sizing = sizing_mod.max_position(o, cfg, scfg)
            except Exception as e:
                _eprint(f"  sizing error: {e}")

    if args.json:
        print(json.dumps([_opp_to_dict(o) for o in opps], indent=2))
        return 0

    if not opps:
        print("No arbitrage candidates found.")
        return 0
    unconfirmed = [o for o in opps if not o.confirmed]
    print(f"\n{len(opps)} candidate(s); {len(realized)} confirmed arbitrage "
          f"(same event + executable edge); {len(unconfirmed)} still "
          f"indicative-only.\n")
    for opp in sorted(opps, key=lambda o: ((o.validation.passed if o.validation
                                            else False), o.realized, o.profit),
                      reverse=True):
        _print_human(opp)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
