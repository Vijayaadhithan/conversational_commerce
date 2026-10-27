"""Business scope decisions without applying rejected shopping-state changes."""

import re

from app.commerce_errors import IntentError
from app.commerce_models import IntentPlan
from app.commerce_text import negated, product_mentions
from app.storefronts import FAMILY_STOREFRONT


def explicit_purchase(message):
    if re.search(
        r"\b(?:code|python|recipe|politics|instructions|ignore|not|no|without|except)\b|don.t",
        message,
        re.I,
    ):
        return False
    return any(
        re.search(
            r"^\s*(?:(?:please|now|i|also|can|could|you)\s+)*(?:need|want|recommend|suggest|looking for)\s+(?:[\w-]+\s+){0,5}"
            + re.escape(term)
            + r"\b",
            message,
            re.I,
        )
        and not negated(term, message)
        for _, _, _, term in product_mentions(message)
    )


def handoff_plan(state, message):
    """An explicit single-section handoff offers the verbatim request without inference."""
    if not state.storefront_id or not explicit_purchase(message):
        return None
    targets = {FAMILY_STOREFRONT[family] for _, _, family, _ in product_mentions(message)}
    if len(targets) == 1 and state.storefront_id not in targets:
        return IntentPlan(
            intent="search", scope="shopping", evidence=message[:500], recommendation_requested=True
        )
    return None


def scope_reply(state, plan, message):
    if plan.scope == "smalltalk" and (plan.intent != "smalltalk" or plan.changes or plan.requests):
        raise IntentError(
            "Smalltalk is only greetings or thanks; product concerns and preference changes are shopping"
        )
    if plan.scope in {"off_topic", "unclear"} and explicit_purchase(message):
        raise IntentError(
            "This explicit product request is shopping, even when switching sections; extract its product and preferences"
        )

    if plan.scope not in {"off_topic", "unclear"}:
        return None
    text = (
        "I can help you shop for clothing, beauty products, footwear, and fashion accessories. "
        "What product or shopping preference would you like help with?"
        if plan.scope == "off_topic"
        else "Could you describe the product or shopping preference you mean? Your saved requirements are unchanged."
    )
    return state, {
        "action": "clarify",
        "reply": text,
        "products": [],
        "search": None,
        "scope": plan.scope,
        "policy_redirect": True,
    }
