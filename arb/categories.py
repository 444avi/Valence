"""Canonical sections shared by Polymarket and Kalshi, and the mapping from each
platform's own taxonomy onto them.

Only markets in the *same* canonical section are ever compared, which both speeds
up matching and removes a whole class of cross-topic false positives.

Platform taxonomies (as exposed by their APIs):
  - Polymarket: events carry `tags`; filterable via /events?tag_slug=<slug>.
  - Kalshi:     events carry a `category` string.

Website sections that are feeds/sorts rather than topics — Polymarket
`trending`/`breaking`, Kalshi `trending`/`mentions` — are intentionally NOT
canonical sections. Kalshi `commodities` folds into `finance`; Kalshi
`climate`/`health` have no Polymarket counterpart and are simply never matched.
"""

from __future__ import annotations

# The buckets both platforms can be checked against each other within.
CANONICAL: list[str] = ["politics", "sports", "crypto", "finance", "tech", "culture"]

# Canonical section -> Polymarket Gamma event tag slugs.
POLYMARKET_TAGS: dict[str, list[str]] = {
    "politics": ["politics", "geopolitics"],
    "sports": ["sports", "esports"],
    "crypto": ["crypto"],
    "finance": ["finance", "business"],
    "tech": ["tech"],
    "culture": ["pop-culture"],
}

# Kalshi API event `category` -> canonical section.
# Unmapped Kalshi categories (Climate and Weather, Health, Transportation) have
# no Polymarket counterpart, so their markets are dropped from matching.
KALSHI_CATEGORIES: dict[str, str] = {
    "Politics": "politics",
    "Elections": "politics",
    "World": "politics",
    "Sports": "sports",
    "Financials": "finance",
    "Economics": "finance",
    "Companies": "finance",
    "Science and Technology": "tech",
    "Entertainment": "culture",
    "Social": "culture",
}

# Crypto has no dedicated Kalshi category (it lives under Financials/Economics),
# and crypto/finance are genuinely different markets — so we promote a
# finance-tagged market to `crypto` when its text is clearly crypto.
_CRYPTO_KEYWORDS = {
    "bitcoin", "btc", "ethereum", "eth", "crypto", "cryptocurrency", "solana",
    "sol", "dogecoin", "doge", "xrp", "ripple", "cardano", "ada", "stablecoin",
    "coinbase", "binance", "altcoin", "memecoin",
}


# Kalshi *game-winner* / match-winner series (markets are the competitors, ±Tie).
# These live deep behind a flood of prop markets (totals, spreads, BTTS, ...) in
# the general feed, so we fetch them directly by series_ticker. Prop series are
# deliberately excluded — only the moneyline/winner markets line up with
# Polymarket's "Will <team> win?" markets. Extend as Kalshi adds leagues.
KALSHI_GAME_SERIES: list[str] = [
    # Soccer (KXWCADVANCE = World Cup knockout "team to advance", incl. ET/pens)
    "KXWCGAME", "KXWCADVANCE", "KXBOLPDIVGAME", "KXISLGAME", "KXUSLGAME",
    "KXUSLCUPGAME",
    # American football
    "KXNFLGAME", "KXNCAAFGAME", "KXCFLGAME", "KXAFLGAME",
    # Baseball
    "KXMLBGAME", "KXKBOGAME", "KXNPBGAME", "KXBSNGAME", "KXVBAGAME",
    # Basketball
    "KXWNBAGAME", "KXACBGAME", "KXNZNBLGAME", "KXLNBELITEGAME",
    # Combat sports
    "KXBOXING", "KXUFCFIGHT",
    # Tennis
    "KXATPMATCH", "KXWTAMATCH", "KXITFMATCH", "KXITFWMATCH",
    "KXATPCHALLENGERMATCH",
    # Cricket / rugby
    "KXWT20MATCH", "KXT20MATCH", "KXRUGBYNRLMATCH",
    # Esports (Polymarket files these under the sports section too)
    "KXCS2GAME", "KXVALORANTGAME", "KXDOTA2GAME", "KXR6GAME", "KXOWGAME",
]


def looks_crypto(text: str) -> bool:
    toks = set(text.lower().replace("?", " ").replace(",", " ").split())
    return bool(toks & _CRYPTO_KEYWORDS)


def kalshi_canonical(category: str | None, text: str = "") -> str:
    """Map a Kalshi event category (+ market text) to a canonical section."""
    canon = KALSHI_CATEGORIES.get((category or "").strip(), "")
    if canon == "finance" and looks_crypto(text):
        return "crypto"
    return canon


def polymarket_canonical(tag_canon: str, text: str = "") -> str:
    """Refine a Polymarket section assignment (crypto can hide under finance)."""
    if tag_canon == "finance" and looks_crypto(text):
        return "crypto"
    return tag_canon


def resolve_sections(requested: list[str] | None) -> list[str]:
    """Validate/normalize a user-requested section list against CANONICAL."""
    if not requested:
        return list(CANONICAL)
    out = []
    for s in requested:
        s = s.strip().lower()
        if s in CANONICAL and s not in out:
            out.append(s)
    return out or list(CANONICAL)
