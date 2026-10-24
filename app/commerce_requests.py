"""Requests for validated shopping conversations."""

import re

from app.commerce_errors import IntentError, PreferenceConflict
from app.commerce_models import Change, TaskRequest
from app.commerce_text import evidence_ok, product_mentions


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
    requests = []
    if plan.requests:
        if len(plan.requests) != len(clauses) or plan.changes:
            raise IntentError(
                "Every product clause must have one request, without unscoped changes"
            )
        for request, clause in zip(plan.requests, clauses):
            if not evidence_ok(request.evidence, clause):
                raise IntentError("Product request omitted or overlapped part of a clause")
            requests.append(request.model_copy(deep=True, update={"evidence": clause}))
    else:
        requests = [TaskRequest(evidence=clause) for clause in clauses]
        for change in plan.changes:
            targets = [
                request for request in requests if evidence_ok(change.evidence, request.evidence)
            ]
            if len(targets) != 1:
                # A combined goal is recovered from the complete literal clauses below.
                if change.field == "query" and len(product_mentions(str(change.value))) > 1:
                    continue
                raise IntentError("Preference has ambiguous product scope")
            targets[0].changes.append(change.model_copy(deep=True))
    for request in requests:
        if any(not evidence_ok(change.evidence, request.evidence) for change in request.changes):
            raise IntentError("Preference evidence belongs to a different product")
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
    return requests
