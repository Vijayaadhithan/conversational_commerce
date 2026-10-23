"""Task selection for a single shopping task."""

import re

from app.commerce_discovery import question, wants_recommendations
from app.commerce_errors import IntentError
from app.commerce_intent import direct_plan, fill_explicit_goal
from app.commerce_models import (
    ShoppingState,
    ShoppingTask,
)
from app.commerce_preferences import (
    SKIN_CONCERN,
    preserve_literal_preferences,
    preserve_unextracted_concerns,
)
from app.commerce_text import (
    evidence_ok,
    matching_families,
    normalized,
    product_mentions,
    task_matches,
)


def normalize_plan(state, plan, message):
    if not evidence_ok(plan.evidence, message):
        raise IntentError("Intent lacks current-message evidence")
    if wants_recommendations(plan) and plan.intent in {"clarify", "smalltalk"}:
        plan.intent = "refine" if state.active() else "search"
    preserve_literal_preferences(plan, message)
    # A facial concern can be mislabelled as a product goal by the model.
    # Keep that literal concern without pretending a concrete product type was chosen.
    for change in plan.changes:
        if (
            change.field == "query"
            and isinstance(change.value, str)
            and not matching_families(change.evidence)
            and re.search(SKIN_CONCERN, change.evidence, re.I)
            and evidence_ok(change.evidence, message)
        ):
            literal = (
                change.value
                if evidence_ok(change.value, change.evidence)
                and not matching_families(change.value)
                else change.evidence
            )
            change.field, change.value = "need", literal
    preserve_unextracted_concerns(state, plan, message)
    if plan.intent == "reset" and not re.search(
        r"start over|reset|forget|new search", normalized(message)
    ):
        existing = state.active()
        repeated = existing and any(evidence_ok(item.value, message) for item in existing.needs)
        if existing and not existing.needs and existing.need:
            repeated = evidence_ok(existing.need, message)
        current_concerns = any(
            change.field == "need"
            and isinstance(change.value, str)
            and evidence_ok(change.value, message)
            for change in plan.changes
        )
        if repeated or current_concerns:
            plan.intent = "refine" if existing else "clarify"
    fill_explicit_goal(plan, message)
    for change in plan.changes:
        if (
            change.field == "query"
            and isinstance(change.value, str)
            and re.fullmatch(r"moisturi\w*", change.value, re.I)
        ):
            face = re.search(
                r"\b(?:face|facial)\s+" + re.escape(change.value) + r"\b", message, re.I
            )
            if face:
                change.value = change.evidence = face.group()
    active = state.active()
    goals = [
        change
        for change in plan.changes
        if change.field == "query" and isinstance(change.value, str)
    ]
    returning = bool(re.search(r"\bback\b|\breturn\b", normalized(message)))
    if returning:
        targets = [task for task in state.tasks if task_matches(task, message)]
        if len(targets) == 1:
            plan.intent, plan.task_id = "switch_task", targets[0].id
    if (
        goals
        and not returning
        and plan.intent
        not in {"reset", "smalltalk", "preferences", "details", "compare", "similar"}
    ):
        families = matching_families(goals[0].value)
        if len(families) > 1:
            state.turns += 1
            return state, question(
                state,
                "Which product should we start with? We can keep separate shopping tasks for the others.",
                "multiple",
            )
        if not active or not active.query:
            plan.intent = "search"
        elif len(families) == 1 and active.filters.family and families[0] != active.filters.family:
            plan.intent = "new_task"
        elif (
            len(families) == 1
            and product_mentions(active.query)
            and active.query != "skin care"
            and normalized(goals[0].value) != normalized(active.query)
            and not re.search(r"\binstead\b", message, re.I)
        ):
            plan.intent = "new_task"
        elif plan.intent == "switch_task":
            plan.intent = "refine"


def select_task(state, plan, message):
    if plan.intent in {"details", "compare"} and plan.changes:
        return state, question(
            state,
            "Would you like to inspect the previously shown products, or search with changed requirements first?",
            "reference",
        )
    if plan.intent == "reset":
        reset = direct_plan(message)
        explicit = re.search(
            r"start over|reset all|forget everything|forget (?:all )?my preferences|new search",
            normalized(message),
        )
        if (not (reset and reset.intent == "reset") and not explicit) or re.search(
            r"don.t|do not|never", normalized(message)
        ):
            raise IntentError("A full reset was not requested")
        state = ShoppingState(turns=state.turns)
        result = question(
            state,
            "What beauty or fashion product are you looking for, and what should it help with?",
            "product_type",
        )
        return state, result
    if plan.intent == "smalltalk":
        return state, {
            "action": "smalltalk",
            "reply": "I'm here to help with beauty and fashion products. Tell me what you need, or what you'd like to change.",
            "products": [],
            "search": None,
        }
    if plan.intent == "switch_task":
        target = next((task for task in state.tasks if task.id == plan.task_id), None)
        terms = (
            set(re.findall(r"\w+", normalized(target.query))) - {"a", "an", "the", "for", "me"}
            if target
            else set()
        )
        if not target or not (
            terms & set(re.findall(r"\w+", normalized(message)))
            or re.search(r"\btask\s*" + str(plan.task_id) + r"\b", normalized(message))
        ):
            return state, question(
                state, "Which saved shopping task would you like to return to?", "reference"
            )
        state.active_task_id = plan.task_id
    if plan.intent == "new_task" and state.active():
        goals = [
            change
            for change in plan.changes
            if change.field == "query" and isinstance(change.value, str)
        ]
        if not state.active().query and not goals:
            plan.intent = "refine"
        elif (
            goals
            and normalized(goals[0].value) == normalized(state.active().query)
            and not re.search(r"new|another|also|now|instead|different|switch", normalized(message))
        ):
            plan.intent = "refine"
    if not state.active() or plan.intent == "new_task":
        if len(state.tasks) >= 5:
            return state, question(
                state,
                "There are five saved shopping tasks. Return to one of them, or explicitly start over.",
            )
        if (
            plan.intent == "new_task"
            and state.tasks
            and not any(change.field == "query" and change.value for change in plan.changes)
        ):
            return state, question(
                state, "Which new product type would you like to look for?", "product_type"
            )
        task = ShoppingTask(id=len(state.tasks) + 1)
        state.tasks.append(task)
        state.active_task_id = task.id
