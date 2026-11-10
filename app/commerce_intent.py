"""Intent for validated shopping conversations."""

import re

from app.commerce_models import FAMILIES, Change, IntentPlan
from app.commerce_recipients import recipient_evidence
from app.commerce_text import (
    evidence_ok,
    matching_families,
    normalized,
    number_text,
    product_mentions,
)


def fill_explicit_goal(plan, message):
    """Recover a literal product type if the model omitted its query change."""
    # Starting a named task and requesting recommendations are separate decisions.
    # Recover its literal goal even when the user is not ready for results yet.
    if plan.recommendation_requested is False and plan.intent != "new_task":
        return
    if plan.intent not in {"search", "refine", "clarify", "new_task", "switch_task"} or any(
        change.field == "query" and change.value for change in plan.changes
    ):
        return
    if re.search(r"\bback\b|\breturn\b", normalized(message)):
        return
    families = matching_families(message)
    if len(families) != 1 or re.search(
        r"\b(?:not|no|without)\b|don.t (?:need|want)", normalized(message)
    ):
        return
    terms = sorted(FAMILIES[families[0]][1], key=len, reverse=True)
    match = re.search(
        r"\b(?:" + "|".join(re.escape(term) for term in terms) + r")\w*", message, re.I
    )
    if match:
        plan.changes = [change for change in plan.changes if change.field != "query"]
        plan.changes.append(Change(field="query", value=match.group(), evidence=match.group()))


def recipient_plan(state, message):
    """Resolve a complete answer to a saved recipient question without model inference."""
    task = state.active()
    if (
        not task
        or task.filters.family not in {"footwear", "apparel"}
        or state.pending_kind != "recipient"
        or len(state.displayed_task_ids) > 1
    ):
        return None
    recipient = recipient_evidence(message)
    if not recipient:
        return None
    literal, department = recipient
    if not re.fullmatch(
        r"(?:(?:it(?:'s| is)|they(?:'re| are)|these are) )?(?:for )?(?:a |an )?"
        + re.escape(normalized(literal)),
        normalized(message).rstrip(".!?"),
    ):
        return None
    return IntentPlan(
        intent="refine",
        scope="shopping",
        evidence=message,
        recommendation_requested=True,
        changes=[
            Change(field="recipient", value=literal, evidence=literal),
            Change(field="attribute.department", value=department, evidence=literal),
        ],
    )


def simple_budget_requests(message):
    """Recognize only complete, separately budgeted product clauses without extra needs."""
    if len(message) > 500:
        return False
    clauses = re.split(r"\s+(?:and|plus)\s+|;\s*", normalized(message).rstrip(".!?"))
    if not 2 <= len(clauses) <= 5:
        return False
    for clause in clauses:
        mentions = product_mentions(clause)
        if len(mentions) != 1:
            return False
        start, end, _, _ = mentions[0]
        if not re.fullmatch(
            r"(?:(?:please )?(?:show(?: me)?|find(?: me)?|recommend(?: me)?|i need|i want) )?"
            r"(?:a |an |some )?",
            clause[:start],
        ) or not re.fullmatch(r" under \$\d{1,7}(?:\.\d{1,2})?", clause[end:]):
            return False
    return True


def direct_plan(message):
    text = normalized(message).rstrip(".!?")
    if simple_budget_requests(message):
        return IntentPlan(intent="search", evidence=message, recommendation_requested=True)
    # UI actions carry an explicit task suffix. Route only otherwise complete
    # inspection/navigation commands; free-form requirements still use the planner.
    scoped = re.fullmatch(r"(.+) (?:for|in) task ([1-5])", text)
    if scoped:
        proposal = direct_plan(scoped.group(1))
        if (
            proposal
            and proposal.task_id is None
            and proposal.intent
            in {
                "more",
                "cheaper",
                "different_brand",
                "compare",
                "details",
            }
        ):
            proposal.evidence = message
            proposal.task_id = int(scoped.group(2))
            return proposal
    action = None
    if re.fullmatch(r"(?:please )?(?:start over|reset|forget my preferences|new search)", text):
        action = "reset"
    elif re.fullmatch(
        r"(?:please )?(?:back|return) to (?:the )?[\w -]+", text
    ) and product_mentions(text):
        action = "switch_task"
    elif text in {"show options", "show me options"}:
        action = "search"
    elif re.fullmatch(
        r"(?:please )?(?:(?:show|give)(?: me)? |any )?(?:more(?: (?:options|choices|products))?|other (?:options|choices)|(?:show )?alternatives)(?: please)?",
        text,
    ):
        action = "more"
    elif re.fullmatch(
        r"(?:please )?(?:show (?:me )?)?(?:cheaper|cheaper options|less expensive options)", text
    ):
        action = "cheaper"
    elif re.fullmatch(
        r"(?:please )?(?:show (?:me )?)?(?:another brand|a different brand|different brands)", text
    ):
        action = "different_brand"
    elif re.fullmatch(
        r"(?:please )?(?:make|set|change|raise|lower) (?:it|that|(?:the |my )?budget)(?: to)? \$?\d+(?:\.\d{1,2})?(?: (?:usd|dollars))?(?: please)?",
        number_text(text),
    ):
        amount = re.search(r"\d+(?:\.\d{1,2})?", number_text(text)).group()
        return IntentPlan(
            intent="refine",
            evidence=message,
            recommendation_requested=True,
            changes=[Change(field="max_price", value=amount, evidence=message)],
        )
    elif text in {"hi", "hello", "hey", "thanks", "thank you"}:
        action = "smalltalk"
    elif text in {"what are my preferences", "what are my requirements", "show my preferences"}:
        action = "preferences"
    elif re.fullmatch(
        r"compare (?:the )?(?:first two|first and second|1 and 2)(?: products| options| ones)?",
        text,
    ):
        action = "compare"
    elif re.fullmatch(
        r"(?:show |tell me |give me )?(?:more )?(?:details|information)(?: about| on| for)? (?:the )?(?:first|second|third|fourth|fifth|[1-5])(?: one| product| option)?",
        text,
    ):
        action = "details"
    elif re.fullmatch(r"details about amazon:[a-z0-9]{10}", text):
        action = "details"
    return (
        IntentPlan(
            intent=action,
            evidence=message,
            recommendation_requested=True
            if action in {"search", "more", "cheaper", "different_brand", "switch_task"}
            else None,
        )
        if action
        else None
    )


def preserve_literal_metal_goal(plan, message):
    """Keep 'silver necklace' literal, using existing title/selected-color eligibility."""
    for changes, clause in [
        (plan.changes, message),
        *[(item.changes, item.evidence) for item in plan.requests],
    ]:
        phrases = []
        for start, end, _, _ in product_mentions(clause):
            prefix = re.search(r"\b(silver|gold)\s+$", clause[:start], re.I)
            if prefix:
                phrases.append((prefix.group(1).casefold(), clause[prefix.start() : end]))
        if len(phrases) != 1:
            continue
        metal, phrase = phrases[0]
        misplaced = [
            change
            for change in changes
            if change.field == "attribute.style"
            and isinstance(change.value, str)
            and normalized(change.value) == metal
            and evidence_ok(change.evidence, clause)
        ]
        if not misplaced:
            continue
        changes[:] = [
            change for change in changes if change not in misplaced and change.field != "query"
        ]
        changes.append(Change(field="query", value=phrase, evidence=phrase))
