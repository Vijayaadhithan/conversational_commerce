"""Task updates for a single shopping task."""

import re

from app.commerce_discovery import question, wants_recommendations
from app.commerce_errors import PreferenceConflict
from app.commerce_models import (
    Change,
)
from app.commerce_preferences import (
    SKIN_CONCERN,
    SKIN_PROFILE,
    apply_changes,
    vague_preference,
)
from app.commerce_text import (
    FOREIGN_CURRENCY,
    evidence_ok,
    matching_families,
    negated,
    normalized,
)


def update_task(state, plan, message, old_pending):
    task = state.active()
    personal_skin_context = old_pending == "skin_profile" or bool(
        re.search(SKIN_PROFILE, message, re.I)
    )
    if personal_skin_context and not re.search(
        r"\b(?:only|must|require|required)\b", message, re.I
    ):
        for change in plan.changes:
            if (
                change.field == "attribute.skin_type"
                and isinstance(change.value, str)
                and evidence_ok(change.value, change.evidence)
            ):
                change.field, change.value = "need", change.evidence
        profile = (
            re.search(r"\b(?:oily|dry|combination|sensitive|normal)(?:\s+skin)?\b", message, re.I)
            if old_pending == "skin_profile"
            else re.search(SKIN_PROFILE, message, re.I)
        )
        if (
            profile
            and not negated(profile.group(), message)
            and not any(
                change.field == "need"
                and isinstance(change.value, str)
                and profile.group().casefold() in change.value.casefold()
                for change in plan.changes
            )
        ):
            plan.changes.append(
                Change(field="need", value=profile.group(), evidence=profile.group())
            )
    proposed = task.model_copy(deep=True)
    try:
        apply_changes(proposed, plan, message)
    except PreferenceConflict as exc:
        return state, question(state, str(exc), "budget_conflict")
    if (
        old_pending == "preference_detail"
        and re.search(r"\b(?:i mean|by that i mean)\b", message, re.I)
        and proposed.filters.attributes.get("fragrance_free") is True
        and re.search(r"\bfragrance[ -]free\b", message, re.I)
        and not negated("fragrance free", message)
    ):
        resolved = [item for item in proposed.needs if vague_preference(item.value)]
        proposed.needs = [
            item
            for item in proposed.needs
            if item not in resolved and normalized(item.value).replace("-", " ") != "fragrance free"
        ]
        proposed.need = "; ".join(item.value for item in proposed.needs)
        if resolved:
            proposed.sources["need"] = {
                "value": proposed.need,
                "items": [item.model_dump() for item in proposed.needs],
                "evidence": message,
                "source": "explicit_user_definition",
            }
            proposed.sources["resolved_preference"] = {
                "previous": [item.model_dump() for item in resolved],
                "value": "fragrance_free",
                "evidence": message,
                "source": "explicit_user_definition",
            }
    state.tasks[state.tasks.index(task)] = proposed
    task = proposed
    if (
        vague_preference(message)
        and not wants_recommendations(plan)
        and not any(change.field == "need" and change.value is None for change in plan.changes)
        and plan.intent not in {"details", "compare", "preferences", "smalltalk"}
    ):
        return state, question(
            state,
            f"I've saved your preference: {task.need}. Which ingredient or property do you want to avoid "
            "(for example, added fragrance)? I won't assume what 'less chemical' or 'gentler' means. "
            "You can also ask me for products now; I'll use this preference as context without claiming the listings verify it.",
            "preference_detail",
        )
    # Requirements not represented by this historical catalog never become implicit matches.
    unsupported_patterns = [
        FOREIGN_CURRENCY,
        r"\bin stock\b|deliver(?:ed|y)? by|arrive by|current price|live price",
        r"\ballerg(?:y|ies|ic)|pregnancy.safe|safe during pregnancy|treat (?:eczema|psoriasis)",
    ]
    for pattern in unsupported_patterns:
        match = re.search(pattern, message, re.I)
        if match and match.group() not in task.unsupported:
            task.unsupported.append(match.group())
    if old_pending == "unsupported" and re.fullmatch(
        r"yes(?: please)?|okay|ok|proceed without (?:that|those)(?: requirements)?",
        normalized(message).rstrip(".!"),
    ):
        task.sources["unsupported"] = {
            "value": [],
            "evidence": message,
            "source": "explicit_answer_to_removal_question",
        }
        task.unsupported = []
    if task.unsupported:
        quoted = ", ".join(task.unsupported)
        return state, question(
            state,
            f"This catalog cannot verify the requirement(s): {quoted}. It contains historical USD listing claims. Would you like to continue without those unverified requirements?",
            "unsupported",
        )
    if plan.question_kind == "multiple" and not matching_families(message):
        plan.question_kind = (
            "skin_profile" if re.search(SKIN_CONCERN, task.need, re.I) else "product_type"
        )
    if plan.intent == "clarify" and plan.question_kind == "multiple":
        return state, question(
            state,
            "Which product should we start with? We can keep separate shopping tasks for the others.",
            "multiple",
        )
    if plan.intent == "preferences":
        return state, {
            "action": "preferences",
            "reply": f"Current goal: {task.query or 'not specified'}. Concern: {task.need or 'not specified'}. Saved requirements: {task.filters.model_dump_json(exclude_none=True)}",
            "products": [],
            "search": None,
        }
