"""Keep product evidence literal while accepting scoped semantic classification."""

import re

from app.commerce_text import evidence_ok, matching_families, negated, normalized
from app.storefronts import STOREFRONTS

CONTEXT_WORDS = set(
    "a an the some something anything item items product products thing things stuff nice good "
    "winter summer spring autumn fall season seasonal warm cold weather rainy monsoon "
    "men man male women woman female adult adults child children boy boys girl girls "
    "for my me i need want please help hiking running dry skin oily sensitive".split()
)


def semantic_product_scope(plan, message, *, storefront_id=None):
    """Classify an explicit unfamiliar product within its planner-selected section.

    The model classifies the user's literal goal, not catalog facts or new requirements.
    Known product mappings stay authoritative; a concern alone is never a product.
    """
    if plan.scope != "shopping" or plan.recommendation_requested is not True:
        return None
    scope = plan.requested_storefront or storefront_id
    section = STOREFRONTS.get(scope, {})
    families = section.get("families", ())
    if not families:
        return None
    goals = [change for change in plan.changes if change.field == "query" and change.value]
    if len(goals) != 1:
        return None
    goal = goals[0]
    if (
        not isinstance(goal.value, str)
        or not evidence_ok(goal.evidence, message)
        or not evidence_ok(goal.value, goal.evidence)
        or negated(goal.value, message)
        or matching_families(goal.value)
        or matching_families(goal.evidence)
        or not (set(re.findall(r"\w+", normalized(goal.value))) - CONTEXT_WORDS)
    ):
        return None
    classified = {
        change.value
        for change in plan.changes
        if change.field == "family"
        and isinstance(change.value, str)
        and change.value in families
        and evidence_ok(change.evidence, message)
        and evidence_ok(goal.value, change.evidence)
    }
    family = (
        families[0] if len(families) == 1 else classified.pop() if len(classified) == 1 else None
    )
    return {"storefront_id": scope, "domain": section["domain"], "family": family}


def semantic_product_family(plan, message, *, storefront_id=None):
    scope = semantic_product_scope(plan, message, storefront_id=storefront_id)
    return scope["family"] if scope else None
