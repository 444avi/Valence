"""Heuristic matching of Polymarket markets to Kalshi markets.

This is the cheap first pass: token-overlap + sequence similarity to surface
plausible same-event pairs. The LLM validator is what actually confirms the
payoffs line up — the heuristic only needs decent recall, not precision.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from functools import lru_cache

from .models import CandidateMatch, Market

_STOPWORDS = {
    "the", "a", "an", "of", "to", "in", "on", "for", "and", "or", "by", "at",
    "be", "is", "are", "will", "would", "this", "that", "with", "as", "it",
    "than", "then", "any", "all", "from", "into", "more", "less", "über",
    "market", "markets", "yes", "no",
}

_TOKEN_RE = re.compile(r"[a-z0-9$%.]+")
_YEAR_RE = re.compile(r"\b(20\d{2})\b")

# Cross-platform team-name aliases (Kalshi says "USA", Polymarket "United
# States", etc.). Expanded in-place so both spellings produce the same tokens.
_ALIASES: dict[str, tuple[str, ...]] = {
    "usa": ("united", "states"),
    "uk": ("united", "kingdom"),
    "uae": ("united", "arab", "emirates"),
}


def _stem(t: str) -> str:
    """Light plural stem so 'advances'/'advance', 'wins'/'win' match."""
    if len(t) > 3 and t.endswith("s") and not t.endswith("ss"):
        return t[:-1]
    return t


@lru_cache(maxsize=8192)
def _normalize(text: str) -> str:
    return text.lower().strip()


@lru_cache(maxsize=8192)
def _token_list(text: str) -> tuple[str, ...]:
    """Ordered token list with aliases expanded and plurals stemmed."""
    out: list[str] = []
    for t in _TOKEN_RE.findall(_normalize(text)):
        for u in _ALIASES.get(t, (t,)):
            out.append(_stem(u))
    return tuple(out)


@lru_cache(maxsize=8192)
def _tokens(text: str) -> frozenset[str]:
    return frozenset(
        t for t in _token_list(text) if t not in _STOPWORDS and len(t) > 1
    )


@lru_cache(maxsize=8192)
def _significant(text: str) -> frozenset[str]:
    """Tokens that carry event identity: numbers, years, and longer words."""
    return frozenset(
        t for t in _tokens(text)
        if any(c.isdigit() for c in t) or len(t) >= 5
    )


def similarity(a: Market, b: Market) -> float:
    """Blend of token Jaccard and sequence ratio over the question text."""
    ta, tb = _tokens(a.question), _tokens(b.question)
    if not ta or not tb:
        return 0.0
    shared = ta & tb
    # Quick reject: no shared tokens => not the same event. Skips the expensive
    # SequenceMatcher for the overwhelming majority of pairs.
    if not shared:
        return 0.0
    jaccard = len(shared) / len(ta | tb)
    seq = SequenceMatcher(None, _normalize(a.question), _normalize(b.question)).ratio()
    score = 0.6 * jaccard + 0.4 * seq

    # A shared significant token (e.g. a name, year, or number) is strong
    # evidence the markets are about the same event; reward it.
    if _significant(a.question) & _significant(b.question):
        score = min(1.0, score + 0.1)

    # Mismatched years almost always means different events — penalize hard.
    ya = set(_YEAR_RE.findall(a.text_blob()))
    yb = set(_YEAR_RE.findall(b.text_blob()))
    if ya and yb and not (ya & yb):
        score *= 0.5

    # Draw/tie vs outright-win is a common same-event trap (e.g. "X vs Y ends in
    # a draw?" matching "X vs Y: X wins"). Those outcomes aren't complementary,
    # so penalize when only one side is a draw/tie market.
    if _is_draw(a.question) != _is_draw(b.question):
        score *= 0.4

    # CRITICAL for "X vs Y" game markets: both teams appear in both questions, so
    # text similarity alone can pair "Iraq wins" with "France wins" — opposite
    # propositions. Buying YES on one + NO on the other is then NOT a hedge but a
    # doubled-down bet. Identify the SUBJECT (the team named as the winner, i.e.
    # the repeated one) and hard-penalize when the subjects disagree.
    na, nb = _normalize(a.question), _normalize(b.question)
    if " vs" in na and " vs" in nb:
        sa = _versus_subject(a.question, shared)
        sb = _versus_subject(b.question, shared)
        if sa and sb and sa != sb:
            score *= 0.25

    return score


_DRAW_WORDS = {"draw", "tie", "drawn", "tied"}


def _is_draw(text: str) -> bool:
    return bool(_tokens(text) & _DRAW_WORDS)


def _versus_subject(text: str, shared: frozenset[str]) -> str | None:
    """The winner-subject of a 'vs' market: the shared token (team) that recurs
    most in the text. "France vs Iraq — Iraq" -> 'iraq'.

    Deterministic: candidates are ranked by (count desc, token asc) so the
    result never depends on set iteration order / PYTHONHASHSEED. On a tie for
    the top count the subject is ambiguous, so we return None and the caller
    abstains from the penalty rather than applying it on a coin flip.
    """
    if not shared:
        return None
    raw = _token_list(text)
    ranked = sorted(
        ((raw.count(tok), tok) for tok in shared),
        key=lambda ct: (-ct[0], ct[1]),
    )
    if len(ranked) >= 2 and ranked[0][0] == ranked[1][0]:
        return None  # tie => ambiguous subject; abstain
    return ranked[0][1]


def _bucket(markets: list[Market]) -> dict[str, list[Market]]:
    out: dict[str, list[Market]] = {}
    for m in markets:
        if m.tradeable() and m.question and m.category:
            out.setdefault(m.category, []).append(m)
    return out


def find_matches(
    polymarket: list[Market],
    kalshi: list[Market],
    threshold: float = 0.4,
    top_k_per_market: int = 2,
) -> list[CandidateMatch]:
    """For each Kalshi market, find its best Polymarket counterpart(s).

    Markets are only compared within the SAME canonical section, which removes
    cross-topic false positives and keeps the comparison cheap. Returns matches
    above `threshold`, sorted by similarity descending.
    """
    pm_buckets = _bucket(polymarket)
    ks_buckets = _bucket(kalshi)

    matches: list[CandidateMatch] = []
    for section, ks_list in ks_buckets.items():
        pm_list = pm_buckets.get(section, [])
        if not pm_list:
            continue
        for k in ks_list:
            scored = sorted(
                ((similarity(p, k), p) for p in pm_list),
                key=lambda x: x[0],
                reverse=True,
            )
            for score, p in scored[:top_k_per_market]:
                if score >= threshold:
                    matches.append(
                        CandidateMatch(polymarket=p, kalshi=k, similarity=score)
                    )

    matches.sort(key=lambda m: m.similarity, reverse=True)
    return matches
