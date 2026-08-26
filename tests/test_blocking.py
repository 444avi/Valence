"""Inverted-index blocking (arb/matcher.token_index + candidates) and the
exactness of arb.max.match_events built on it.

The index restricts comparison to markets that share >=1 token. Because
similarity() returns 0.0 for any pair with no shared token, this must never
change which pairs clear the threshold: match_events over the index has to equal
the old brute-force all-pairs scan, tie-breaks included.
"""

from arb import matcher, max as maxmod
from arb.models import CandidateMatch, Market


def M(platform: str, mid: str, q: str) -> Market:
    return Market(platform, mid, q, "", 0.5, 0.5, category=platform)


def _brute(small, large, threshold):
    """The pre-fix all-pairs inner loop, kept here as the equivalence oracle."""
    matched, unmatched = {}, []
    for ev_title, ev_markets in small.items():
        best = None
        for sm in ev_markets:
            for lg in large:
                score = matcher.similarity(sm, lg)
                if score < threshold:
                    continue
                if best is None or score > best.similarity:
                    if sm.platform == "polymarket":
                        best = CandidateMatch(polymarket=sm, kalshi=lg, similarity=score)
                    else:
                        best = CandidateMatch(polymarket=lg, kalshi=sm, similarity=score)
        if best is not None:
            matched[ev_title] = best
        else:
            unmatched.append(ev_title)
    return matched, unmatched


def _norm(matched):
    return {k: (v.polymarket.market_id, v.kalshi.market_id, round(v.similarity, 9))
            for k, v in matched.items()}


def test_candidates_only_token_sharing_in_original_order():
    large = [
        M("kalshi", "k0", "Trump wins the 2028 election"),   # shares trump/2028/election
        M("kalshi", "k1", "Rain in Seattle tomorrow"),        # shares nothing
        M("kalshi", "k2", "2028 Democratic nominee"),         # shares 2028
    ]
    idx = matcher.token_index(large)
    cands = matcher.candidates(idx, M("polymarket", "p0", "Will Trump win in 2028?"))
    ids = [m.market_id for m in cands]
    assert "k1" not in ids                 # no shared token -> excluded
    assert set(ids) == {"k0", "k2"}
    assert ids == ["k0", "k2"]             # returned in the large-list order


def test_no_shared_token_yields_no_candidates():
    idx = matcher.token_index([M("kalshi", "k", "apple banana orange")])
    assert matcher.candidates(idx, M("polymarket", "p", "zebra")) == []


def test_indexed_match_events_equals_bruteforce():
    small = {
        "T": [M("polymarket", "p_trump", "Will Trump win the 2028 election?")],
        "D": [M("polymarket", "p_dem", "2028 Democratic nominee announced")],
        "X": [M("polymarket", "p_x", "Total lunar eclipse fully visible")],  # no pair
    }
    large = [
        M("kalshi", "k_trump", "Trump wins 2028 election"),
        M("kalshi", "k_dem", "Democratic 2028 nominee"),
        M("kalshi", "k_noise", "Bitcoin above 100k this year"),
    ]
    got_m, got_u = maxmod.match_events(small, large, 0.4)
    exp_m, exp_u = _brute(small, large, 0.4)

    assert _norm(got_m) == _norm(exp_m)
    assert set(got_u) == set(exp_u)
    # And the intended shape: two real matches, the eclipse left unmatched.
    assert set(got_m) == {"T", "D"}
    assert got_u == ["X"]
    assert got_m["T"].kalshi.market_id == "k_trump"
    assert got_m["D"].kalshi.market_id == "k_dem"


def test_similarity_ge_is_exact_at_threshold():
    """similarity_ge returns the exact score for pairs at/above threshold and
    something below it otherwise — the invariant that makes the quick_ratio gate
    safe to use inside match_events."""
    from arb.matcher import similarity, similarity_ge
    pairs = [
        (M("polymarket", "a", "Will Trump win the 2028 election?"),
         M("kalshi", "b", "Trump wins 2028 election")),                 # high
        (M("polymarket", "c", "Fed cuts rates at the March 2026 meeting"),
         M("kalshi", "d", "Will the Fed cut rates in March 2026?")),    # high
        (M("polymarket", "e", "Lakers beat the Celtics tonight"),
         M("kalshi", "f", "Bitcoin closes above 100k in 2026")),        # ~0
        (M("polymarket", "g", "2028 Democratic presidential nominee"),
         M("kalshi", "h", "2028 Republican presidential nominee")),     # borderline
    ]
    for a, b in pairs:
        exact = similarity(a, b)
        for thr in (0.2, 0.4, 0.6, 0.8):
            g = similarity_ge(a, b, thr)
            if exact >= thr:
                assert abs(g - exact) < 1e-9, (a.question, b.question, thr, exact, g)
            else:
                assert g < thr, (a.question, b.question, thr, exact, g)
