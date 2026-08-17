"""LLM validation of candidate arbitrage opportunities.

Uses Claude Sonnet 4.6 at low effort to judge the ONE thing the price math
can't: do the two markets resolve on the *same* underlying event, with YES/NO
meaning the same outcome, so that buying YES on one and NO on the other is a
genuine hedge? Structured JSON output keeps it machine-readable.

It is deliberately NOT asked whether "an arbitrage exists": the model has no
independent view of the fees or executable prices, so asking it to re-confirm
the arithmetic would only launder a wrong fee model into a false positive.
Whether an arb exists is decided downstream by the (independent) fee math plus
CLOB price confirmation.
"""

from __future__ import annotations

import json

import anthropic

from .models import ArbOpportunity, Validation

MODEL = "claude-sonnet-4-6"

_SYSTEM = """You are a careful prediction-market resolution analyst.

You are given one market from Polymarket and one from Kalshi that a heuristic \
flagged as possibly the same event. Judge ONLY their resolution criteria — do \
NOT reason about prices, fees, or whether a profit exists (you have no reliable \
view of those, and they are computed independently downstream).

Validate, conservatively:
1. same_event: Do both markets resolve based on the SAME underlying real-world \
outcome? Watch for differences in date/timeframe, threshold/strike, geography, \
exact entity, or resolution source (e.g. "wins in regulation" vs "advances by \
any method including penalties") that would make them NOT equivalent.
2. equivalent_payoff: Does "YES" mean the same thing on both sides, so that \
buying YES on one and NO on the other is genuinely hedged — exactly one leg \
pays $1 in EVERY resolution, including edge cases (draws, ties, cancellation, \
extra time)? If the wordings are negations or only partially overlap, false.

Be skeptical. If resolution criteria differ in any way that could cause the two \
markets to settle differently, set equivalent_payoff false. Prefer false \
negatives over false positives — a wrong "yes" loses real money.

Respond ONLY with the JSON object matching the requested schema."""

_SCHEMA = {
    "type": "object",
    "properties": {
        "same_event": {"type": "boolean"},
        "equivalent_payoff": {"type": "boolean"},
        "confidence": {
            "type": "number",
            "description": "0.0-1.0 confidence in this assessment",
        },
        "reasoning": {"type": "string"},
        "caveats": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "same_event",
        "equivalent_payoff",
        "confidence",
        "reasoning",
        "caveats",
    ],
    "additionalProperties": False,
}


def _build_prompt(opp: ArbOpportunity) -> str:
    pm = opp.match.polymarket
    ks = opp.match.kalshi
    proposed = " + ".join(f"{leg.platform} {leg.side}" for leg in opp.legs)
    return f"""POLYMARKET market:
  question: {pm.question}
  resolution/description: {pm.description or "(none provided)"}

KALSHI market:
  question: {ks.question}
  resolution/rules: {ks.description or "(none provided)"}

The proposed hedge would be: {proposed}. Judge only whether these two markets \
resolve on the same event with equivalent payoffs — not whether it is \
profitable."""


def validate(
    opp: ArbOpportunity, client: anthropic.Anthropic | None = None
) -> Validation:
    client = client or anthropic.Anthropic()
    resp = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        system=_SYSTEM,
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": _SCHEMA}},
        messages=[{"role": "user", "content": _build_prompt(opp)}],
    )
    text = next((b.text for b in resp.content if b.type == "text"), "{}")
    data = json.loads(text)
    return Validation(
        same_event=bool(data["same_event"]),
        equivalent_payoff=bool(data["equivalent_payoff"]),
        confidence=float(data["confidence"]),
        reasoning=str(data["reasoning"]),
        caveats=list(data.get("caveats", [])),
    )
