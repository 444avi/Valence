"""LLM validation of candidate arbitrage opportunities.

Uses Claude Haiku 5.5 at high effort, falling back to Claude Sonnet 4.6, to
judge the ONE thing the price math can't: does YES on the Polymarket market
mean the same outcome as YES on the Kalshi market, so that buying YES on one
and NO on the other is a genuine hedge? Structured JSON output keeps it
machine-readable.

It is deliberately NOT asked whether "an arbitrage exists", and it is not shown
the proposed legs: the model has no independent view of the fees or executable
prices, and reasoning through crossed YES/NO payoffs is where it makes logic
errors. It only compares the two YES conditions; whether an arb exists is
decided downstream by the (independent) fee math plus CLOB price confirmation.

The schema puts the two YES conditions and the reasoning BEFORE the verdict so
the model commits to a verdict only after spelling out what each market means;
with the verdict first it would sometimes contradict its own reasoning.
"""

from __future__ import annotations

import json
import logging

import anthropic

from .models import ArbOpportunity, Validation

log = logging.getLogger(__name__)

MODEL = "claude-haiku-5-5"
EFFORT = "high"
FALLBACK_MODEL = "claude-sonnet-4-6"
FALLBACK_EFFORT = "medium"
# Thinking counts toward max_tokens; at high effort a verdict uses ~750 tokens
# typically and up to ~2.3k in testing, so 1024 would truncate about a third.
MAX_TOKENS = 8192
# Fail over to the fallback model instead of waiting out a stuck request.
TIMEOUT_S = 120.0

# Part of the validation-cache key (see valcache.question_hash): bump it
# whenever the prompt, schema, or model changes so old verdicts are not reused.
PROMPT_VERSION = "v7-haiku55"

_SYSTEM = """You check whether two prediction markets, one on Polymarket and one on Kalshi, are bets on the same outcome.

How your answer is used: a program has matched these two markets by their titles and will treat them as interchangeable, buying YES on one and NO on the other so that exactly one position pays out whatever happens. That only works if YES on the Polymarket market and YES on the Kalshi market are the same outcome. Your job is to confirm or reject that. Prices, fees, and profit are handled elsewhere.

How to work:
1. For each market, state in plain words what has to happen for it to resolve YES, for example "the Giants win the Oct 11 game" or "a Republican is inaugurated as Nevada governor after the 2026 election". Use the question and title to identify which event, race, office, or body the market is about, and the rules to identify the condition. Kalshi rules usually state only the YES condition; anything else resolves NO.
2. Compare the two YES conditions. They match when, in the normal course of the event, they are true in exactly the same outcomes. They don't match when they name different teams, candidates, parties, or sides of a line; when one is the opposite of the other; or when they differ in timeframe, threshold, office, geography, or what counts (winning in regulation vs. advancing on penalties, any Senate vote vs. the final-passage vote).

What not to weigh: the trader accepts the risk of rare, abnormal resolutions, so differences that only matter when the event doesn't play out normally don't count against a match. That covers cancellations and postponements, deadlines passing with no result, Polymarket's "Other" and 50-50 resolutions, rare ties, walkovers and retirements, vacant or interim titles, and a race being called versus the winner being inaugurated. Note any you see in caveats.

Fields:
- polymarket_yes, kalshi_yes: each market's YES condition, in plain words.
- same_event: both markets are about the same underlying event (the same game, race, award, or measurement), whichever side each one takes.
- equivalent_payoff: the two YES conditions are true in exactly the same outcomes in the normal course of the event.

Illustrative examples (not from your inputs):
- Polymarket "Lakers vs. Celtics: Lakers" (YES = the Lakers win) and Kalshi "Lakers vs Celtics: Boston" (YES = the Celtics win): same_event true, equivalent_payoff false, because they take opposite sides of the same game.
- Polymarket "Will Smith win the 2026 Masters?", which resolves "Other" if the tournament is cancelled, and Kalshi "If Smith wins the 2026 Masters, resolves Yes": same_event true, equivalent_payoff true, with the cancellation clause noted in caveats.
- Polymarket "Will the bill pass the House by June 30?" and Kalshi "Will the bill become law in 2026?": same_event false, equivalent_payoff false, because passing one chamber is not becoming law and the timeframes differ."""

_SCHEMA = {
    "type": "object",
    "properties": {
        "polymarket_yes": {"type": "string"},
        "kalshi_yes": {"type": "string"},
        "reasoning": {"type": "string"},
        "caveats": {"type": "array", "items": {"type": "string"}},
        "same_event": {"type": "boolean"},
        "equivalent_payoff": {"type": "boolean"},
        "confidence": {
            "type": "number",
            "description": "0.0-1.0 confidence in this assessment",
        },
    },
    "required": [
        "polymarket_yes",
        "kalshi_yes",
        "reasoning",
        "caveats",
        "same_event",
        "equivalent_payoff",
        "confidence",
    ],
    "additionalProperties": False,
}


def _build_prompt(opp: ArbOpportunity) -> str:
    pm = opp.match.polymarket
    ks = opp.match.kalshi
    return f"""POLYMARKET market:
  question: {pm.question}
  rules: {pm.description or "(none provided)"}

KALSHI market:
  question: {ks.question}
  rules: {ks.description or "(none provided)"}"""


class _NoVerdict(Exception):
    """The model answered but produced no usable verdict (refusal, truncation)."""


def _ask(client: anthropic.Anthropic, model: str, effort: str,
         opp: ArbOpportunity) -> Validation:
    resp = client.with_options(timeout=TIMEOUT_S).messages.create(
        model=model,
        max_tokens=MAX_TOKENS,
        system=_SYSTEM,
        thinking={"type": "adaptive"},
        output_config={"effort": effort, "format": {"type": "json_schema", "schema": _SCHEMA}},
        messages=[{"role": "user", "content": _build_prompt(opp)}],
    )
    if resp.stop_reason in ("refusal", "max_tokens"):
        raise _NoVerdict(f"{model} stopped with {resp.stop_reason}")
    text = next((b.text for b in resp.content if b.type == "text"), None)
    if text is None:
        raise _NoVerdict(f"{model} returned no text")
    data = json.loads(text)
    return Validation(
        same_event=bool(data["same_event"]),
        equivalent_payoff=bool(data["equivalent_payoff"]),
        confidence=float(data["confidence"]),
        reasoning=str(data["reasoning"]),
        caveats=list(data.get("caveats", [])),
        model=model,
    )


def _call_model(
    opp: ArbOpportunity, client: anthropic.Anthropic | None = None
) -> Validation:
    """One real verdict: the primary model, or the fallback if the primary
    refuses, truncates, returns unparseable output, or the API call fails.
    No caching — this is the cost-bearing path. Raises if both fail."""
    client = client or anthropic.Anthropic()
    try:
        return _ask(client, MODEL, EFFORT, opp)
    except (anthropic.APIError, _NoVerdict, json.JSONDecodeError, KeyError) as e:
        log.warning("validator: %s failed (%s); falling back to %s",
                    MODEL, e, FALLBACK_MODEL)
    return _ask(client, FALLBACK_MODEL, FALLBACK_EFFORT, opp)


def validate(
    opp: ArbOpportunity, client: anthropic.Anthropic | None = None
) -> Validation:
    """Validate a pair, consulting the cross-run cache when one is configured.

    With no cache configured (VALENCE_DB unset — the CLI and test default) this
    is exactly a single `_call_model`. When the web job runner configures a
    cache, a hit returns the stored verdict for free and a miss calls the model,
    stores the verdict, and increments the run's real-call counter — so the
    counter reflects actual API calls, never cache hits (see arb/valcache.py).
    """
    from . import valcache

    cache = valcache.from_env()
    if cache is None:
        return _call_model(opp, client)
    try:
        hit = cache.lookup(opp)
        if hit is not None:
            return hit
        result = _call_model(opp, client)  # cost incurred only here
        cache.store(opp, result, result.model or MODEL)
        return result
    finally:
        cache.close()
