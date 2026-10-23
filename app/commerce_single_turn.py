"""Ordered validation and state transitions for a single shopping task."""

from app.commerce_models import ShoppingState
from app.commerce_recommendations import recommend_or_inspect
from app.commerce_task_selection import normalize_plan, select_task
from app.commerce_task_updates import update_task


def _handle_single_turn(saved, plan, message, search):
    state = ShoppingState.model_validate(saved or {}).model_copy(deep=True)
    result = normalize_plan(state, plan, message)
    if result is not None:
        return result
    old_pending = state.pending_kind
    state.pending_question, state.pending_kind = "", ""
    state.turns += 1
    result = select_task(state, plan, message)
    if result is not None:
        return result
    result = update_task(state, plan, message, old_pending)
    if result is not None:
        return result
    return recommend_or_inspect(state, plan, message, old_pending, search)
