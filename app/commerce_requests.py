"""Requests for validated shopping conversations."""

import re

from app.commerce_errors import IntentError, PreferenceConflict
from app.commerce_models import Change, TaskRequest
from app.commerce_text import FOREIGN_CURRENCY, amounts, evidence_ok, product_mentions


def split_product_requests(message):
    mentions = product_mentions(message)
    if len(mentions) < 2:
        return []
    boundaries = []
    for previous, current in zip(mentions, mentions[1:]):
        between = message[previous[1] : current[0]]
        separators = list(
            re.finditer(
                r"\s+(?:and|plus|as well as|also)\s+|\s*&\s*|[;,]\s*|[.!?]\s+", between, re.I
            )
        )
        if not separators:
            continue
        separator = separators[-1]
        boundaries.append((previous[1] + separator.start(), previous[1] + separator.end()))
    starts = [0] + [end for _, end in boundaries]
    ends = [start for start, _ in boundaries] + [len(message)]
    return [message[start:end].strip(" \t\n,;") for start, end in zip(starts, ends)]


def shared_need_context(plan, message, clauses):
    """Expand a coordinated bare-product list using its literal trailing concern.

    The scope text can join source spans, but every Change still quotes the
    original message. Never share prices, recipients, hard filters, or concerns
    across separately qualified products or separate sentences.
    """
    mentions = product_mentions(message)
    if any(
        re.search(r"[;.!?]", message[left[1] : right[0]])
        for left, right in zip(mentions, mentions[1:])
    ):
        return clauses, ""

    def bare_product(text):
        items = product_mentions(text)
        if len(items) != 1:
            return False
        start, end, _, term = items[0]
        prefix = text[:start]
        if term.casefold().startswith(("conditioner", "shampoo")):
            prefix = re.sub(r"\bhair\s*$", "", prefix, flags=re.I)
        return not text[end:].strip() and bool(
            re.fullmatch(
                r"(?:(?:i|we|need|want|would|like|please|show|find|recommend|suggest|get|me|us|a|an|the|some|and|also)\s+)*",
                prefix,
                flags=re.I,
            )
        )

    if not all(bare_product(clause) for clause in clauses[:-1]):
        return clauses, ""
    last = product_mentions(clauses[-1])
    if len(last) != 1:
        return clauses, ""
    end = last[0][1]
    match = re.fullmatch(r"\s+for\s+(.+?)\s*[.!?]?", clauses[-1][end:], re.I)
    if not match or not bare_product(clauses[-1][:end]):
        return clauses, ""
    need = match.group(1).strip()
    changes = [*plan.changes, *(c for request in plan.requests for c in request.changes)]
    supported = [c for c in changes if evidence_ok(c.evidence, need)]
    if (
        not supported
        or any(c.field != "need" or not isinstance(c.value, str) for c in supported)
        or amounts(need)
        or re.search(FOREIGN_CURRENCY, need, re.I)
        or not any(evidence_ok(c.value, need) for c in supported)
    ):
        return clauses, ""
    suffix = " for " + need
    return [clause + suffix for clause in clauses[:-1]] + [clauses[-1]], need


def multi_requests(plan, message):
    clauses = split_product_requests(message)
    if len(clauses) < 2:
        if plan.requests:
            if len(plan.requests) > 1 and len(product_mentions(message)) > 1:
                raise IntentError("Multiple requests need separate explicit product clauses")
            # A planner may wrap several concerns for one named product (or
            # the current task) in requests. Flatten only unambiguous wrappers;
            # the usual field/value checks still reject invented requirements.
            combined = list(plan.changes)
            for request in plan.requests:
                if not evidence_ok(request.evidence, message):
                    raise IntentError("Product request lacks current-message evidence")
                if any(
                    not evidence_ok(change.evidence, request.evidence) for change in request.changes
                ):
                    raise IntentError("Preference evidence belongs to a different product")
                combined.extend(request.changes)
            plan.changes = list({(c.field, c.value, c.evidence): c for c in combined}.values())
            if len(plan.changes) > 20:
                raise IntentError("Too many preference changes for one task")
            plan.requests = []
        return []
    if plan.intent in {"details", "compare", "similar", "reset", "preferences", "smalltalk"}:
        return []
    if any(len(clause) > 500 for clause in clauses):
        raise PreferenceConflict(
            "Please send each product's requirements in a separate message so I can keep all the details."
        )
    # Shared constraints need a scope decision, not an arbitrary per-item allocation.
    if re.search(
        r"\b(?:both|each|all|total|combined|together|between them|respectively)\b", message, re.I
    ):
        raise PreferenceConflict(
            "I can keep each product in a separate shopping task. Please specify the budget and requirements for each product; I won't assume how a shared requirement or total budget should be split."
        )
    clauses, shared_need = shared_need_context(plan, message, clauses)
    if any(len(clause) > 500 for clause in clauses):
        raise PreferenceConflict("Please send each product's requirements in a separate message.")

    def shared_change(change):
        return (
            bool(shared_need)
            and change.field == "need"
            and isinstance(change.value, str)
            and evidence_ok(change.value, shared_need)
            and evidence_ok(change.evidence, shared_need)
        )

    # Models can put the first product in top-level changes and later ones in
    # requests, or emit requests out of order. Scope by verified source evidence,
    # never array position, and keep every explicitly named product clause.
    requests = [TaskRequest(evidence=clause) for clause in clauses]
    assigned = set()
    for proposal in plan.requests:
        targets = [i for i, clause in enumerate(clauses) if evidence_ok(proposal.evidence, clause)]
        if len(targets) != 1 or targets[0] in assigned:
            raise IntentError("Product request omitted or overlapped part of a clause")
        index = targets[0]
        assigned.add(index)
        requests[index].changes.extend(change.model_copy(deep=True) for change in proposal.changes)
    for change in plan.changes:
        if shared_change(change):
            for request in requests:
                request.changes.append(change.model_copy(deep=True))
            continue
        targets = [
            request for request in requests if evidence_ok(change.evidence, request.evidence)
        ]
        if len(targets) != 1:
            # Recover a combined product goal from the literal clauses below.
            if change.field == "query" and len(product_mentions(str(change.value))) > 1:
                continue
            raise IntentError("Preference has ambiguous product scope")
        targets[0].changes.append(change.model_copy(deep=True))
    for request in requests:
        if any(
            not evidence_ok(change.evidence, request.evidence)
            or not evidence_ok(change.evidence, message)
            for change in request.changes
        ):
            raise IntentError("Preference evidence belongs to a different product")
        if shared_need:
            # Keep the complete literal concern once, rather than storing both
            # "dry hair", "frizz" and "dry hair and frizz" as three concerns.
            request.changes = [change for change in request.changes if not shared_change(change)]
            request.changes.append(Change(field="need", value=shared_need, evidence=shared_need))
        # Preserve unextracted relevance text, without copying price constraints
        # into long-lived concerns (which would become stale on budget changes).
        if not any(change.field == "need" for change in request.changes):
            remainder = request.evidence
            remainder = re.sub(
                r"\b(?:under|below|less than|up to|at most|over|more than|at least|budget(?: is| of)?)\s*\$?\s*\d+(?:\.\d+)?(?:\s*(?:USD|dollars))?",
                " ",
                remainder,
                flags=re.I,
            )
            for change in request.changes:
                remainder = re.sub(re.escape(change.evidence), " ", remainder, flags=re.I)
            for _, _, _, term in product_mentions(request.evidence):
                remainder = re.sub(re.escape(term), " ", remainder, flags=re.I)
            remainder = re.sub(
                r"\b(?:i|a|an|the|need|want|please|show|recommend|suggest|find|get|me|also|for|and|now|instead)\b",
                " ",
                remainder,
                flags=re.I,
            ).strip(" ,.;")
            if remainder.strip():
                # A residual phrase is only used if it remains an actual quote.
                if not evidence_ok(remainder, request.evidence):
                    raise IntentError(
                        "Some product requirements were not extracted; supply separate literal changes"
                    )
                request.changes.append(Change(field="need", value=remainder, evidence=remainder))
        request.changes = list(
            {(c.field, c.value, c.evidence): c for c in request.changes}.values()
        )
        if len(request.changes) > 20:
            raise IntentError("Too many preference changes for one task")
    return requests
