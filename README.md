# Polymarket × Kalshi Arbitrage Finder

Finds risk-free arbitrage opportunities across [Polymarket](https://polymarket.com)
and [Kalshi](https://kalshi.com) binary prediction markets.

It pulls from the **global Polymarket network** (`gamma-api.polymarket.com` — the
USDC-settled markets, not the US-regulated venue) and the public Kalshi API.

It works in stages:

1. **Heuristic match** — pull live markets from both platforms, bucket them into
   shared **canonical sections**, and pair them by text similarity (token
   overlap + sequence ratio, with year/number guards) *within the same section
   only*. Cheap, high-recall, deliberately noisy.
2. **Two-phase pricing** — *screen* every candidate on Polymarket's Gamma
   mid-quotes plus a spread haircut (cheap, wide, clearly **indicative**), then
   *confirm* the survivors' real executable best-ask from the Polymarket CLOB
   order book before any LLM spend. Fees are modeled per platform and per
   category (see [Fees](#fees)). A number is only ever reported as realizable
   profit once its prices are CLOB-confirmed.
3. **LLM validation** — [Claude Sonnet 4.6](https://www.anthropic.com) at **low
   effort** judges only what the math can't: do the two markets resolve on the
   *same* event with YES/NO meaning the same thing (so the hedge is real)? It is
   deliberately **not** asked whether a profit exists — it has no independent
   view of fees or prices, so that decision stays with the (independent) fee
   math + price confirmation. This kills the heuristic's false positives (e.g.
   "Will X be next PM?" vs "Who will *succeed* X?").

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # then put your ANTHROPIC_API_KEY in .env
```

> ⚠️ **Rotate your API key** if it was ever pasted into a chat or shared. `.env`
> is gitignored, but a leaked key should be revoked at the Anthropic Console.

## Usage

```bash
.venv/bin/python -m arb                                  # scan all sections
.venv/bin/python -m arb --sections politics,crypto       # scope to sections (deeper each)
.venv/bin/python -m arb --sections sports --per-section 400
.venv/bin/python -m arb --no-llm                         # skip Claude; raw priced candidates
.venv/bin/python -m arb --json > out.json                # machine-readable output
```

## Sections

Markets are only ever compared **within the same canonical section**, which
removes cross-topic false positives and keeps matching cheap. Each platform's
own sections are mapped onto these shared buckets:

| Canonical | Polymarket sections | Kalshi sections |
|-----------|---------------------|-----------------|
| `politics` | politics, geopolitics | politics, elections, world |
| `sports`   | sports, esports | sports |
| `crypto`   | crypto | crypto markets (under financials/economics) |
| `finance`  | finance, business | finance, economics, companies, commodities |
| `tech`     | tech | tech & finance (science & technology) |
| `culture`  | pop-culture | culture (entertainment, social) |

Feeds/sorts rather than topics — Polymarket `trending`/`breaking`, Kalshi
`trending`/`mentions` — are **not** sections. Kalshi `climate`/`health` have no
Polymarket counterpart and are never matched. The full mapping lives in
[arb/categories.py](arb/categories.py).

Coverage scales inversely with how many sections you scan: `--limit` is split
evenly across the selected sections, so `--sections politics` digs much deeper
into politics than a full six-section run at the same `--limit`. Use
`--per-section` to set depth directly.

### Sports game-winner coverage

Kalshi's per-game moneyline markets (e.g. "Argentina vs Austria → Argentina")
sit buried behind thousands of prop markets in the general feed, so when
`sports` is in scope the tool also fetches the **game-winner series directly by
ticker** (`KXWCGAME`, `KXNFLGAME`, `KXMLBGAME`, tennis/UFC/esports, …; see
[arb/categories.py](arb/categories.py)). Disable with `--no-games`.

Because both teams appear in both platforms' wording for a game, matching needs
two extra guards so it never proposes a *fake* hedge (these are in
[arb/matcher.py](arb/matcher.py)):

- **Reversed-subject guard** — "X vs Y → **X** wins" must not pair with
  "X vs Y → **Y** wins". Buying YES on one and NO on the other there is doubling
  down, not hedging. The matcher identifies the winner-subject (the repeated
  team) and hard-penalizes a mismatch.
- **Draw/tie guard** — a "draw"/"tie" market only pairs with another draw/tie
  market, never with an outright-win market.

Note that thin cross-platform edges on game lines usually don't survive Kalshi's
~2¢ per-contract fee, so expect most to price out as small *losses* — that's the
fee-aware math working, not a missed opportunity.

## Max mode — exhaustive one-category coverage

```bash
.venv/bin/python -m arb.max --section crypto            # comprehensive check
.venv/bin/python -m arb.max --section tech --no-llm     # free: heuristic only
.venv/bin/python -m arb.max --section politics --max-validations 30
.venv/bin/python -m arb.max --section politics --min-volume 1000   # wider, slower
.venv/bin/python -m arb.max --section sports --json > sports_coverage.json
```

Where the batch scanner samples, `arb.max` is **comprehensive within one
section** ([arb/max.py](arb/max.py)):

1. Fetch *everything* in the section from both platforms (no caps; Gamma's
   ~2000-event pagination limit is the only bound).
2. **Drop untradeable dust** below `--min-volume` (default **$10k**). A market
   with no volume can never be an executable arb leg, yet on a big section the
   dust is the overwhelming majority of the comparison cost — politics alone is
   ~34k markets, roughly **half of them $0-volume** (median ~$19). The floor is
   clamped to $1 so the $0 dust is always excluded; lower `--min-volume` toward
   $1 for wider (and much slower) coverage.
3. Count distinct **events** per platform; the platform with fewer events is
   the "small" side.
4. For **every** event on the small side, find the best heuristic counterpart
   market on the large side (same matcher + guards; default threshold 0.4,
   looser than the batch scanner since the LLM checks everything). Matching is
   **blocked on shared tokens** and skips the expensive sequence-ratio on any
   pair a cheap upper bound already rules out, so it stays near-linear instead of
   comparing all pairs — an *exact* optimization (identical results), just fast
   enough that a full politics run finishes in minutes instead of hours (see
   [arb/matcher.py](arb/matcher.py)).
5. LLM-verify **every** matched pair — same-event + equivalent-payoff + arb at
   current prices. Uncapped by default; the match count is printed before
   verification starts as a cost signal (`--max-validations N` caps it,
   highest-profit pairs first).

The report classifies each small-side event as `[ARB ✅]` (equivalent and
profitable now), `[equivalent]` (verified same market — worth monitoring),
`[different ❌]`, or unmatched. `--json` emits the whole catalog including the
unmatched list.

### Max-mode options

| Flag | Default | Meaning |
|------|---------|---------|
| `--section` | required | One of `politics,sports,crypto,finance,tech,culture` |
| `--min-volume` | 10000 | Drop markets below this volume ($) before matching; floor $1 |
| `--similarity` | 0.4 | Heuristic match threshold (looser; LLM gates after) |
| `--max-validations` | 0 (all) | Cap LLM calls, highest-profit pairs first |
| `--size` | 1 | Order size (contracts) for per-contract economics |
| `--pm-haircut` | 0.02 | Spread haircut on PM mids before CLOB confirmation |
| `--no-confirm` | off | Skip CLOB confirmation (report stays UNCONFIRMED) |
| `--impact-buffer` | 0.75 | Cap sizing at this fraction of thinner-leg depth |
| `--gas-cost` | 0.0 | Fixed $/round-trip (Polygon gas), amortized in sizing |
| `--withdrawal-cost` | 0.0 | Fixed $/round-trip (USDC withdrawal), amortized |
| `--no-sizing` | off | Skip order-book position sizing on confirmed arbs |
| `--kalshi-fee` | 0.07 | Kalshi taker fee rate |
| `--polymarket-fee` | (table) | Override every PM category with this flat rate |
| `--no-llm` | off | Heuristic matching only |
| `--json` | off | JSON catalog (matched + unmatched) |

## Options

### Batch scanner (`python -m arb`)

| Flag | Default | Meaning |
|------|---------|---------|
| `--limit` | 720 | Max markets per platform, split across sections |
| `--per-section` | — | Max markets per section per platform (overrides `--limit`) |
| `--sections` | all | Comma list: `politics,sports,crypto,finance,tech,culture` |
| `--no-games` | off | Skip targeted Kalshi sports game-winner coverage |
| `--similarity` | 0.45 | Heuristic match threshold (0–1) |
| `--min-profit` | 0.0 | Minimum net profit/contract (dollars) to report |
| `--size` | 1 | Order size (contracts) for per-contract economics |
| `--pm-haircut` | 0.02 | Spread haircut on PM mids before CLOB confirmation |
| `--no-confirm` | off | Skip CLOB confirmation (report stays UNCONFIRMED) |
| `--max-validations` | 15 | Cap on Claude validation calls (cost control) |
| `--impact-buffer` | 0.75 | Cap sizing at this fraction of thinner-leg depth |
| `--gas-cost` | 0.0 | Fixed $/round-trip (Polygon gas), amortized in sizing |
| `--withdrawal-cost` | 0.0 | Fixed $/round-trip (USDC withdrawal), amortized |
| `--no-sizing` | off | Skip order-book position sizing on confirmed arbs |
| `--kalshi-fee` | 0.07 | Kalshi taker fee rate |
| `--polymarket-fee` | (table) | Override every PM category with this flat rate |
| `--no-llm` | off | Skip validation, report raw priced candidates |
| `--json` | off | Emit JSON instead of human-readable output |

## How the arbitrage works

For a binary event that resolves identically on both platforms, buy YES on one
and NO on the other. Exactly one leg pays $1 regardless of outcome, so if the
combined cost (prices + fees) is below $1, the profit is locked in:

```
profit = 1 - (yes_ask_A + no_ask_B + fee_A/size + fee_B/size)
```

The tool computes both directions (PM-YES/KS-NO and KS-YES/PM-NO) and keeps the
better one.

**Reversed-polarity guard.** Both directions assume the two markets are phrased
with the same YES-polarity — "YES here" and "YES there" mean the same outcome.
The matcher only guarantees the same *topic*, so it will pair Polymarket "Will
the Democrats win the WI governor race?" with Kalshi "Will the Republican party
win?" — the same event from opposite sides. There, Kalshi-YES ("Republican
wins") is the same bet as Polymarket-NO ("Democrats lose"), so "buy Kalshi YES +
Polymarket NO" doubles one position instead of hedging, and the two cheap
same-side legs masquerade as a fat arb. The engine detects this from the quotes
— on a lopsided market the four prices fit the reversed pairing far better than
the same-side one — and reports no arbitrage. Near 50/50, where a genuine arb
and a reversed listing are numerically identical, it abstains and leans on the
LLM validator instead ([arb/arbitrage.py](arb/arbitrage.py)).

## Fees

Fee models are verified against each platform's August 2026 schedule and are
configurable ([arb/fees.py](arb/fees.py)):

- **Kalshi**: `ceil(rate × C × P × (1−P))` rounded to the cent **once per
  order** (not per contract — per-contract rounding overstates by up to ~36%).
  Rate 0.07 for takers; a per-series override table exists (all known series are
  0.07). At `--size 1` the per-contract fee is an *upper bound* and is labeled
  as such; pass the real `--size` for true economics.
- **Polymarket**: `shares × rate × P × (1−P)` — a symmetric curve peaking at
  P=0.50 and near zero at the extremes (NOT linear in price). Rate is
  per-category: politics/finance/tech 0.04; sports/culture 0.05; crypto 0.07;
  geopolitics/world 0.00. Markets with `feesEnabled=false` pay **zero**
  regardless of category. `--polymarket-fee R` overrides every category with R.

## Prices: indicative vs confirmed

Polymarket's Gamma `outcomePrices` are mid/last quotes whose two sides sum to
exactly $1 — *not* executable asks (each is ~half a spread too cheap). The batch
and max pipelines therefore screen on mids + a `--pm-haircut` (default 0.02/leg)
and then confirm the survivors' true best-ask from the CLOB `/book` before
pricing. Anything still priced from mids is marked `~` on the leg and labeled
**UNCONFIRMED** — it never appears as realizable profit. Kalshi quotes are real
order-book asks, so they are confirmed from the start.

## Position sizing

A positive spread at top-of-book says an arb *exists*; it says nothing about how
much you can put on. For every confirmed, same-event arb the tool walks **both
full order books** and reports a max executable size ([arb/sizing.py](arb/sizing.py)):

- **Edge-exhaustion ceiling** — as size grows you eat deeper levels, so the
  *average* fill price on each leg rises. At each size the fee-adjusted spread is
  recomputed from those averages (net of Kalshi trading fees and Polymarket
  trading + fixed gas/withdrawal costs); the size where it crosses zero is the
  ceiling. Because fixed costs and Kalshi's ceil-to-cent make *tiny* sizes
  unprofitable too, the whole curve is scanned — the ceiling is the largest
  profitable size, not just the first crossing.
- **Depth ceiling (thinner leg)** — total depth available at or better than the
  edge-exhaustion price on each leg, taking the min (the arb is bottlenecked by
  the thinner book), then a configurable **market-impact buffer**
  (`--impact-buffer`, default 0.75) caps usage below 100% of visible depth.

The recommended size is `min(edge-exhaustion, buffer × depth-ceiling)`. Order
books come from the Polymarket CLOB `/book` and Kalshi's orderbook (where buying
YES means consuming the resting NO bids at `ask = 1 − bid`). Flags:
`--impact-buffer`, `--gas-cost`, `--withdrawal-cost`, `--no-sizing`. Example line:

```
SIZE: max ~150 pairs  (edge-exhaustion 152, depth ceiling 200, book cap 200)
      ~$148 deployed -> $0.63 profit; avg fills YES 0.467 / NO 0.517
```

## Important caveats

- **Slippage beyond the sized depth and resolution timing differences are not
  modeled.** A CLOB-confirmed best-ask is one price level; the sizing walk
  accounts for depth, but latency between the two legs is on you.
- **The LLM judges resolution criteria only** — same event + equivalent payoff —
  never the arithmetic. It prefers false negatives over false positives; a
  passing verdict is a strong signal, not a guarantee. Read the reasoning.

## Architecture

```
arb/
  categories.py  # canonical sections + per-platform taxonomy mapping
  sources.py     # fetch + normalize markets (Polymarket Gamma, Kalshi /events)
  matcher.py     # heuristic text matching (token-index blocking) -> candidate pairs
  fees.py        # platform fee models
  arbitrage.py   # two-leg arb math, net of fees
  validator.py   # Claude Sonnet 4.6 (low effort) structured validation
  models.py      # dataclasses
  cli.py         # batch pipeline: fetch -> match -> screen -> confirm -> validate
  max.py         # exhaustive one-section coverage: smaller platform fully checked
  sizing.py      # order-book walk: edge-exhaustion + depth-ceiling max position
tests/           # pytest: fees, matcher determinism, pricing, validator, sizing
```

Run the tests with `.venv/bin/python -m pytest tests/ -q`.

Not financial advice. For research/educational use.
