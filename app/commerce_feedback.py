"""Resolve displayed references and retain literal, task-scoped product feedback."""

import re

from app.commerce_discovery import question
from app.commerce_errors import IntentError
from app.commerce_models import ProductFeedback
from app.commerce_text import evidence_ok, reference_positions


def resolved_positions(task, message):
    # Money is never a display position, including single-digit prices.
    references = re.sub(
        r"\$\s*\d+(?:\.\d+)?|\b\d+(?:\.\d+)?\s*(?:USD|dollars)\b", "", message, flags=re.I
    )
    positions = reference_positions(references, len(task.shown))
    literal_ids = re.findall(r"amazon:[A-Z0-9]{10}", message)
    if literal_ids:
        return [i + 1 for i, row in enumerate(task.shown) if row["product_id"] in literal_ids]
    if positions:
        return positions
    # An explicit but invalid number must not silently become the focused product.
    if re.search(
        r"\b(?:option|product|number)\s*\d+\b|\b(?:sixth|seventh|eighth|ninth|tenth)\b",
        message,
        re.I,
    ):
        return []
    if re.search(r"\b(?:these|those|all (?:of )?them)\b", message, re.I):
        return list(range(1, len(task.shown) + 1))
    focused = [i for i in task.focused_positions if 1 <= i <= len(task.shown)]
    if len(focused) == 1 and re.search(r"\b(?:it|this|that|same|one)\b", message, re.I):
        return focused
    return [1] if len(task.shown) == 1 else []


def rejected_ids(task):
    latest = {}
    for feedback in task.feedback:
        latest.update(dict.fromkeys(feedback.product_ids, feedback.sentiment))
    return [key for key, value in latest.items() if value == "dislike"]


def record_feedback(state, plan, message, old_pending=""):
    if not plan.feedback and plan.intent != "feedback":
        return None
    task = state.active()
    if not plan.feedback or not evidence_ok(plan.evidence, message):
        raise IntentError(
            "Product feedback needs a supported sentiment and current-message evidence"
        )
    negative = re.search(
        r"\b(?:dislike|hate|reject|not (?:for me|my style)|too \w+)\b|don.t (?:like|want)|do not (?:like|want)",
        message,
        re.I,
    )
    positive = re.search(
        r"\b(?:like|love|prefer)\b",
        message[: negative.start()] + message[negative.end() :] if negative else message,
        re.I,
    )
    if (
        (negative and positive)
        or re.search(
            r"\bnot too\b|don.t (?:dislike|hate|reject)|do not (?:dislike|hate|reject)",
            message,
            re.I,
        )
        or (plan.feedback == "dislike" and not negative)
    ) or (plan.feedback == "like" and (not positive or negative)):
        raise IntentError("Feedback sentiment is ambiguous; clarify without changing preferences")
    positions = resolved_positions(task, message)
    if (
        not positions
        and old_pending == "feedback"
        and task.feedback
        and not re.search(
            r"\b(?:option|product|first|second|third|fourth|fifth|sixth|it|this|that|one)\b|\d|amazon:",
            message,
            re.I,
        )
    ):
        # A literal reason answering our feedback question belongs to that
        # reaction's products, not whichever product the model guesses.
        previous = task.feedback[-1].product_ids
        positions = [i + 1 for i, row in enumerate(task.shown) if row["product_id"] in previous]
    if not positions or any(not 1 <= i <= len(task.shown) for i in positions):
        return question(state, "Which displayed option do you mean?", "reference")
    reaction_scope = (
        message[: negative.start()] + message[negative.end() :] if negative else message
    )
    if len(positions) > 1 and re.search(
        r"\b(?:but|except|over|rather than|not|to)\b", reaction_scope, re.I
    ):
        return question(state, "Which single option should I save that reaction for?", "reference")
    item = ProductFeedback(
        sentiment=plan.feedback,
        evidence=message,
        product_ids=[task.shown[i - 1]["product_id"] for i in positions],
    )
    if not task.feedback or task.feedback[-1] != item:
        if len(task.feedback) >= 20:
            return question(
                state,
                "This task has 20 saved reactions. Start over to clear them before saving more.",
                "feedback_limit",
            )
        task.feedback.append(item)
    task.focused_positions = positions
    if plan.intent == "feedback" or plan.recommendation_requested is False:
        return question(
            state,
            "I've saved your reaction to those options. What would you like to change?"
            if plan.feedback == "dislike"
            else "I've saved the options you like. Would you like similar options or more details?",
            "feedback",
        )
    return None
