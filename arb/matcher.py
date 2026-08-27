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

# Tokenization is memoized per question string. The bound must comfortably
# exceed the number of DISTINCT questions in a single run, or a large
# same-section sweep (e.g. `arb.max --section politics`, ~34k markets) thrashes
# the cache and recomputes tokens on nearly every similarity() call. It stays
# bounded (not maxsize=None) so the long-lived `arb.live` monitor can't grow it
# without limit as markets churn over days.
_CACHE_MAX = 1 << 18  # 262144

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


@lru_cache(maxsize=_CACHE_MAX)
def _normalize(text: str) -> str:
    return text.lower().strip()


@lru_cache(maxsize=_CACHE_MAX)
def _token_list(text: str) -> tuple[str, ...]:
    """Ordered token list with aliases expanded and plurals stemmed."""
    out: list[str] = []
    for t in _TOKEN_RE.findall(_normalize(text)):
        for u in _ALIASES.get(t, (t,)):
            out.append(_stem(u))
    return tuple(out)


@lru_cache(maxsize=_CACHE_MAX)
def _tokens(text: str) -> frozenset[str]:
    return frozenset(
        t for t in _token_list(text) if t not in _STOPWORDS and len(t) > 1
    )


@lru_cache(maxsize=_CACHE_MAX)
def _significant(text: str) -> frozenset[str]:
    """Tokens that carry event identity: numbers, years, and longer words."""
    return frozenset(
        t for t in _tokens(text)
        if any(c.isdigit() for c in t) or len(t) >= 5
    )


@lru_cache(maxsize=_CACHE_MAX)
def _years(text: str) -> frozenset[str]:
    """Distinct 20xx years in a market's text blob, cached per market so the
    regex over (often long) resolution descriptions runs once, not once per pair.
    That per-pair regex was a big share of similarity()'s cost on large sweeps."""
    return frozenset(_YEAR_RE.findall(text))


def _finish_score(a: Market, b: Market, shared: frozenset[str],
                  jaccard: float, seq: float) -> float:
    """Combine token Jaccard + sequence ratio into the final score, applying the
    significant-token bonus and the year / draw / versus penalties. Split out of
    similarity() so the quick_ratio gate (similarity_ge) can share it once it has
    decided the pair is worth the exact ratio()."""
    score = 0.6 * jaccard + 0.4 * seq

    # A shared significant token (e.g. a name, year, or number) is strong
    # evidence the markets are about the same event; reward it.
    if _significant(a.question) & _significant(b.question):
        score = min(1.0, score + 0.1)

    # Mismatched years almost always means different events — penalize hard.
    ya, yb = _years(a.text_blob()), _years(b.text_blob())
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
    # the repeated one) and hard-penalize when the two markets' subjects are
    # disjoint. Subjects are token SETS so multi-word teams ("South Africa") are
    # compared as a unit rather than lost to an internal tie.
    na, nb = _normalize(a.question), _normalize(b.question)
    if " vs" in na and " vs" in nb:
        sa = _versus_subjects(a.question, shared)
        sb = _versus_subjects(b.question, shared)
        if sa and sb and sa.isdisjoint(sb):
            score *= 0.25

    return score


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
    return _finish_score(a, b, shared, jaccard, seq)


def similarity_ge(a: Market, b: Market, threshold: float) -> float:
    """Exact similarity(a, b) when it is >= threshold; otherwise a value < threshold.

    Skips the O(n^2) SequenceMatcher.ratio() whenever the cheap O(n) quick_ratio()
    upper bound already proves the score can't reach `threshold`. quick_ratio() >=
    ratio(), and every adjustment in _finish_score except the +0.1 significant-token
    bonus only *reduces* the score, so `0.6*jaccard + 0.4*quick_ratio (+0.1)` is a
    true upper bound on the final score. Matching that only cares whether a pair
    clears a positive threshold therefore gets results identical to similarity().
    """
    ta, tb = _tokens(a.question), _tokens(b.question)
    if not ta or not tb:
        return 0.0
    shared = ta & tb
    if not shared:
        return 0.0
    jaccard = len(shared) / len(ta | tb)
    sm = SequenceMatcher(None, _normalize(a.question), _normalize(b.question))
    ub = 0.6 * jaccard + 0.4 * sm.quick_ratio()
    if _significant(a.question) & _significant(b.question):
        ub = min(1.0, ub + 0.1)
    if ub < threshold:
        return 0.0
    return _finish_score(a, b, shared, jaccard, sm.ratio())


_DRAW_WORDS = {"draw", "tie", "drawn", "tied"}


def _is_draw(text: str) -> bool:
    return bool(_tokens(text) & _DRAW_WORDS)


def _versus_subjects(text: str, shared: frozenset[str]) -> frozenset[str]:
    """The winner-subject of a 'vs' market: the shared token(s) repeated MOST in
    the text (the winner is appended, so its tokens recur). "France vs Iraq Iraq"
    -> {'iraq'}.

    Returns a SET, not a single token, so a MULTI-WORD team survives intact:
    "Namibia vs South Africa South Africa" -> {'south', 'africa'}. The old
    single-token version tied 'south' and 'africa' and abstained, which let
    "Namibia wins" match "South Africa wins" — opposite propositions dressed up
    as an arbitrage. Empty when nothing is repeated (top count 1): no side is
    singled out as the winner, so the caller abstains.

    Deterministic: membership is by count value, independent of set iteration
    order / PYTHONHASHSEED.
    """
    if not shared:
        return frozenset()
    raw = _token_list(text)
    counts = {tok: raw.count(tok) for tok in shared}
    top = max(counts.values())
    if top < 2:
        return frozenset()  # nothing repeated => no discernible winner-subject
    return frozenset(tok for tok, c in counts.items() if c == top)


def _bucket(markets: list[Market]) -> dict[str, list[Market]]:
    out: dict[str, list[Market]] = {}
    for m in markets:
        if m.tradeable() and m.question and m.category:
            out.setdefault(m.category, []).append(m)
    return out


# --- Blocking (candidate generation) ---------------------------------------
# similarity() returns 0.0 for any pair that shares no post-stopword token (the
# quick-reject inside it). So an exhaustive all-pairs scan spends almost all of
# its work scoring pairs that can only ever score zero — quadratic and, on a big
# same-section set like politics (~21k x ~12k = 260M pairs), effectively
# non-terminating. These two helpers let a caller score each market against only
# the markets that share >=1 token with it. It is an EXACT optimization: every
# pair it skips would have scored 0.0 (below any threshold > 0), so the set of
# matches above threshold is unchanged.

def token_index(markets: list[Market]) -> dict[str, list[tuple[int, Market]]]:
    """Inverted index: token -> [(position, market), ...] for markets carrying it.

    Keyed on the same `_tokens` that `similarity` uses for its shared-token test,
    so the candidates it drives are exactly the markets that can score > 0.
    Positions are the market's index in `markets`; `candidates` uses them to
    return hits in the original list order, preserving the exact tie-break of a
    plain `for lg in markets` scan (first-seen wins on equal similarity).
    """
    idx: dict[str, list[tuple[int, Market]]] = {}
    for pos, m in enumerate(markets):
        for tok in _tokens(m.question):
            idx.setdefault(tok, []).append((pos, m))
    return idx


def candidates(index: dict[str, list[tuple[int, Market]]], m: Market) -> list[Market]:
    """Markets in `index` sharing >=1 token with `m`, de-duplicated and returned
    in the indexed list's original order.

    Any market NOT returned shares no token with `m` and would score exactly 0.0
    in `similarity`, so skipping it never changes a match above threshold > 0.
    """
    seen: set[int] = set()
    hits: list[tuple[int, Market]] = []
    for tok in _tokens(m.question):
        for pos, cand in index.get(tok, ()):
            if pos not in seen:
                seen.add(pos)
                hits.append((pos, cand))
    hits.sort(key=lambda pm: pm[0])
    return [cand for _, cand in hits]


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
