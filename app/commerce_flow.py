"""Flow for validated shopping conversations."""

import re

from app.commerce_discovery import question
from app.commerce_errors import IntentError, PreferenceConflict
from app.commerce_models import IntentPlan, ShoppingState, ShoppingTask
from app.commerce_requests import multi_requests
from app.commerce_single_turn import _handle_single_turn
from app.commerce_text import evidence_ok, product_mentions, task_matches


def handle_turn(saved, plan, message, search):
    state = ShoppingState.model_validate(saved or {}).model_copy(deep=True)
    if not evidence_ok(plan.evidence, message):
        raise IntentError("Intent lacks current-message evidence")
    if (
        state.active()
        and re.search(r"\ballerg(?:y|ies|ic)\b", message, re.I)
        and re.search(r"\b(?:would|are|is|can|guarantee|guaranteed)\b", message, re.I)
        and not re.search(r"\b(?:only|must|require|need|want)\b", message, re.I)
    ):
        state.turns += 1
        return state, question(
            state,
            "These listings cannot guarantee allergy safety. Fragrance-free or sensitive-skin wording does not establish that a product is safe for your allergies. Ingredient lists may be incomplete or outdated. Which ingredient are you concerned about? I can show what the listings explicitly state and flag missing information; your saved requirements are unchanged.",
            "evidence_limit",
        )
    try:
        requests = multi_requests(plan, message)
    except PreferenceConflict as exc:
        state.turns += 1
        return state, question(state, str(exc), "multiple")
    if requests:
        targets = []
        for request in requests:
            matches = [task for task in state.tasks if task_matches(task, request.evidence)]
            fresh = re.search(r"\b(?:new|another|also|separate)\b", request.evidence, re.I)
            if len(matches) > 1 and not fresh:
                state.turns += 1
                return state, question(
                    state,
                    "Several saved tasks have that product type. Please specify the task numbers to change.",
                    "task_selection",
                )
            targets.append(matches[0].id if len(matches) == 1 and not fresh else None)
        if len(state.tasks) + targets.count(None) > 5:
            state.turns += 1
            return state, question(
                state,
                "This request would exceed five saved shopping tasks. Return to a saved task or explicitly start over; no products from this request have been added.",
                "multiple",
            )
        groups = []
        for request, target in zip(requests, targets):
            subplan = IntentPlan(
                intent="new_task",
                evidence=request.evidence[:500],
                changes=request.changes,
                recommendation_requested=plan.recommendation_requested,
            )
            # Separate tasks even for two products in the same family; restore named tasks.
            task = next((task for task in state.tasks if task.id == target), None)
            if task is None:
                task = ShoppingTask(id=len(state.tasks) + 1)
                state.tasks.append(task)
            state.active_task_id = task.id
            state.pending_kind, state.pending_question = task.pending_kind, task.pending_question
            subplan.intent = "search"
            state, result = _handle_single_turn(
                state.model_dump(mode="json"), subplan, request.evidence, search
            )
            active = state.active()
            active.pending_kind, active.pending_question = (
                state.pending_kind,
                state.pending_question,
            )
            products = [dict(row, task_id=active.id) for row in result["products"]]
            groups.append(dict(result, products=products, task_id=active.id, query=active.query))
        state.turns = ShoppingState.model_validate(saved or {}).turns + 1
        state.displayed_task_ids = [group["task_id"] for group in groups]
        state.pending_question, state.pending_kind = (
            "Name the product you want to refine or inspect.",
            "task_selection",
        )
        reply = "\n\n".join(
            f"Task {group['task_id']} — {group['query'] or 'Product request'}\n{group['reply']}"
            for group in groups
        )
        reply += "\n\nName the product or task number when asking for changes, more options, or product details."
        return state, {
            "action": "multi",
            "reply": reply,
            "products": [row for group in groups for row in group["products"]],
            "groups": groups,
            "search": None,
        }
    mentions = product_mentions(message)
    targets = [task for task in state.tasks if task_matches(task, message)]
    explicit_task = re.search(r"\btask\s+([1-5])\b", message, re.I)
    if explicit_task:
        targets = [task for task in state.tasks if task.id == int(explicit_task.group(1))]
    fresh = bool(re.search(r"\b(?:new|another|also|separate)\b", message, re.I))
    if len(targets) > 1 and not fresh:
        state.turns += 1
        return state, question(
            state,
            "Several saved tasks have that product type. Which task number do you mean?",
            "task_selection",
        )
    if len(targets) == 1 and not fresh and plan.intent not in {"reset", "smalltalk", "preferences"}:
        if re.search(r"\b(?:back|return)\b", message, re.I):
            plan.changes = [c for c in plan.changes if c.field != "query"]
        state.active_task_id = targets[0].id
        state.pending_kind, state.pending_question = (
            targets[0].pending_kind,
            targets[0].pending_question,
        )
        if plan.intent in {"new_task", "switch_task", "search"}:
            plan.intent = "refine"
    elif (
        (len(state.displayed_task_ids) > 1 or explicit_task)
        and not fresh
        and not mentions
        and plan.intent not in {"reset", "smalltalk", "preferences"}
    ):
        state.turns += 1
        return state, question(
            state,
            "Which product or task number do you mean? Each product has its own saved requirements and numbered options.",
            "task_selection",
        )
    state, result = _handle_single_turn(state.model_dump(mode="json"), plan, message, search)
    if state.active():
        state.active().pending_kind, state.active().pending_question = (
            state.pending_kind,
            state.pending_question,
        )
        if result["action"] not in {"smalltalk", "preferences"}:
            state.displayed_task_ids = [state.active_task_id]
    return state, result
