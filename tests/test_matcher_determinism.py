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


def test_versus_subject_abstains_on_tie():
    from arb.matcher import _versus_subject, _tokens
    # Both teams appear once -> tie -> None (caller applies no penalty).
    shared = _tokens("Arsenal vs Chelsea")
    assert _versus_subject("Arsenal vs Chelsea", shared) is None
    # Clear winner (repeated team) -> that team.
    assert _versus_subject("Arsenal vs Chelsea Arsenal", shared) == "arsenal"
