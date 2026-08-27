"""Matcher tie-break determinism (defect 3).

_versus_subject used to break ties on frozenset iteration order, so identical
inputs scored differently across PYTHONHASHSEED values. It must now be
hash-seed-independent.
"""

import subprocess
import sys

_SNIPPET = """
from arb.matcher import similarity
from arb.models import Market

def M(q):
    return Market("polymarket", "i", q, "", 0.5, 0.5, category="sports")

# Ambiguous tie case from the defect report: the second market names both
# teams once each, so the winner-subject is ambiguous and must be abstained on.
a = M("Arsenal vs Chelsea Will Arsenal win?")
b = M("Arsenal vs Chelsea")
print(f"{similarity(a, b):.6f}")
"""


def _score_with_seed(seed: str) -> str:
    out = subprocess.run(
        [sys.executable, "-c", _SNIPPET],
        capture_output=True, text=True,
        env={"PYTHONHASHSEED": seed, "PATH": ""},
        cwd=".",
    )
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def test_similarity_is_hash_seed_independent():
    scores = {_score_with_seed(s) for s in ("0", "1", "42", "1000", "123456")}
    assert len(scores) == 1, f"non-deterministic across seeds: {scores}"


def test_versus_subjects_identify_repeated_team():
    from arb.matcher import _versus_subjects, _tokens
    # Nothing repeated -> no winner singled out -> empty (caller applies no penalty).
    shared = _tokens("Arsenal vs Chelsea")
    assert _versus_subjects("Arsenal vs Chelsea", shared) == frozenset()
    # Clear winner (repeated team) -> that team.
    assert _versus_subjects("Arsenal vs Chelsea Arsenal", shared) == frozenset({"arsenal"})
    # A MULTI-WORD winner survives as a set instead of tying to nothing.
    sh2 = _tokens("Namibia vs South Africa")
    assert _versus_subjects("Namibia vs South Africa South Africa", sh2) == frozenset(
        {"south", "africa"}
    )


def test_reversed_subject_with_multiword_team_is_rejected():
    """Regression: 'Namibia wins' vs 'South Africa wins' are OPPOSITE outcomes and
    must not match as an arb. The reversed-subject guard used to abstain here
    because 'South'/'Africa' tied, so the pair matched at ~0.63 and got confirmed
    as a fake $5k arbitrage. With set-valued subjects it is hard-penalized."""
    from arb.matcher import similarity
    from arb.models import Market

    def M(plat, q):
        return Market(plat, "i", q, "", 0.5, 0.5, category="sports")

    pm = M("polymarket", "Namibia vs South Africa Namibia")   # Namibia to win
    ks = M("kalshi", "Namibia vs South Africa South Africa")   # South Africa to win
    assert similarity(pm, ks) < 0.4, f"reversed subjects should be rejected: {similarity(pm, ks)}"
    # Same winner on both sides must still match strongly.
    ks_same = M("kalshi", "Namibia vs South Africa Namibia")
    assert similarity(pm, ks_same) >= 0.4
