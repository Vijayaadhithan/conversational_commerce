"""Discovery for validated shopping conversations."""

import re

from app.commerce_models import ShoppingState
from app.commerce_preferences import SKIN_CONCERN, SKIN_PROFILE


def question(state, text, kind="general"):
    state.pending_question, state.pending_kind = text, kind
    return {"action": "clarify", "reply": text, "products": [], "search": None}


def intent_clarification(saved, message):
    """Reject the proposal, but keep the conversation usable and its state intact."""
    state = ShoppingState.model_validate(saved or {}).model_copy(deep=True)
    state.turns += 1
    active = state.active()
    context = (
        f" for {active.query or active.need}" if active and (active.query or active.need) else ""
    )
    result = question(
        state,
        f"I couldn't reliably interpret that change{context}. Your saved requirements are unchanged. "
        "Please name the product and the preference you want to add, change, or remove.",
        "intent_repair",
    )
    result["validation_recovered"] = True
    return state, result


def wants_recommendations(plan):
    # The semantic decision takes precedence over action labels. Legacy callers
    # can still supply a search intent without the newer explicit decision.
    if plan.recommendation_requested is not None:
        return plan.recommendation_requested
    return plan.intent in {"search", "more", "cheaper", "different_brand", "similar"}


def skin_discovery(state, task, message, old_pending, plan):
    """A concern can guide broad discovery without inventing a specific product type."""
    if task.query or not re.search(SKIN_CONCERN, task.need, re.I):
        return None
    requested = plan.recommendation_requested is True or (
        plan.recommendation_requested is None and bool(old_pending) and wants_recommendations(plan)
    )
    answered = bool(
        old_pending in {"skin_profile", "preference_detail", "intent_repair"}
        and (
            re.search(SKIN_PROFILE, task.need, re.I)
            or re.search(r"\b(?:oily|dry|combination|sensitive|normal)\b", message, re.I)
        )
    )
    if requested or (answered and plan.recommendation_requested is None):
        task.query = "skin care"
        task.filters.domain, task.filters.family = "beauty", "skin_care"
        task.sources["query"] = {
            "value": task.query,
            "source": "concern_guided_discovery",
            "evidence": message,
            "basis": task.need,
            "specific_product_type_selected": False,
        }
        for name, value in (("domain", "beauty"), ("family", "skin_care")):
            task.sources[name] = {
                "value": value,
                "source": "concern_guided_discovery",
                "basis": task.need,
            }
        return None
    if not re.search(SKIN_PROFILE, task.need, re.I):
        missing = "What is your skin type (oily, dry, combination, sensitive, or unsure)?"
    elif task.filters.max_price is None:
        missing = "Do you have a maximum budget in USD, or would you prefer to browse without one?"
    else:
        missing = "Would you like to browse skin-care options with these preferences?"
    return question(
        state,
        f"I've saved your concerns: {task.need}. {missing}"
        + (" You can also ask to browse now." if task.filters.max_price is None else ""),
        "skin_profile",
    )


def preference_question(task):
    """Ask about one useful missing preference without turning it into a requirement."""
    if task.shown:
        return "What would you like to change about these options?"
    if task.filters.family in {"apparel", "footwear"} and not any(
        name in task.filters.attributes for name in ("department", "age_range")
    ):
        return "Who are these for (for example adult men, adult women, or children)?"
    if task.filters.max_price is None:
        return "Do you have a maximum budget in USD, or would you prefer to browse without one?"
    return "Would you like to see matching products, or change a preference first?"


def no_results_reply(task, more=False):
    """Offer an explicit next step using the actual constraints; never relax them."""
    prefix = f"I found no {'unseen ' if more else ''}{task.query} listings matching all your saved requirements. "
    if more:
        return (
            prefix + "Your requirements are unchanged. Would you like to revisit a previous option?"
        )
    choices = []
    if task.filters.max_price is not None or task.filters.min_price is not None:
        choices.append("budget")
    if task.filters.brand or task.filters.exclude_brands:
        choices.append("brand")
    choices.extend(name.replace("_", " ") for name in task.filters.attributes)
    followup = (
        "Which requirement would you like to change: " + ", ".join(choices[:3]) + "?"
        if choices
        else "Would you like to try a different product type or description?"
    )
    return prefix + "Your requirements are unchanged. " + followup
