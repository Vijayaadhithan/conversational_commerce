"""Conversation decisions and semantic retrieval, separate from hard requirements."""

import re
from difflib import SequenceMatcher

from app.commerce_text import evidence_ok
from app.search_normalization import normalize_search
from app.storefronts import FAMILY_STOREFRONT, STOREFRONTS


def reconcile_concerns(task, plan, message):
    """A clarified spelling supersedes its earlier duplicate; keep the source history."""
    latest = [
        change.value
        for change in plan.changes
        if change.field == "need"
        and isinstance(change.value, str)
        and evidence_ok(change.evidence, message)
    ]
    if not latest:
        return

    def words(text):
        return re.findall(r"\w+", text.casefold())

    def covered(old, new):
        if len(words(old)) != 1:
            return False
        return all(
            any(
                normalize_search(a) == normalize_search(b)
                or (
                    len(a) >= 4
                    and len(b) >= 4
                    and a[:2] == b[:2]
                    and SequenceMatcher(None, a, b).ratio() >= 0.8
                )
                for b in words(new)
            )
            for a in words(old)
        )

    replaced = [
        item
        for item in task.needs
        if item.value not in latest and any(covered(item.value, new) for new in latest)
    ]
    if replaced:
        history = task.sources.get("resolved_concerns", {}).get("items", [])
        task.sources["resolved_concerns"] = {
            "items": (history + [item.model_dump() for item in replaced])[-20:],
            "source": "preference_clarification",
            "evidence": message,
        }
        task.needs = [item for item in task.needs if item not in replaced]
        task.need = "; ".join(item.value for item in task.needs)
        task.sources["need"] = {
            "value": task.need,
            "source": "combined_user_concerns",
            "items": [item.model_dump() for item in task.needs],
        }


def browsing_paused(message):
    return bool(
        re.fullmatch(r"\s*(?:please )?(?:wait|pause|hold on|stop)[.!\s]*", message, re.I)
        or re.search(
            r"\b(?:not (?:yet|now)|(?:just|only) (?:asking|discussing|saving|talking)|"
            r"don['’]?t (?:search|show|recommend|browse|suggest)|do not (?:search|show|recommend|browse|suggest)|"
            r"stop (?:showing|searching|browsing|suggesting)|no (?:more )?(?:products|suggestions|recommendations))\b",
            message,
            re.I,
        )
    )


def continue_browsing(state, plan, message):
    """A preference/rejection during browsing is an instruction to improve results."""
    if browsing_paused(message):
        if plan.intent in {"search", "refine", "clarify", "feedback", "more"}:
            plan.recommendation_requested = False
        return
    explicit_goal = (
        state.storefront_id
        and plan.scope == "shopping"
        and plan.intent in {"search", "refine", "clarify"}
        and re.search(
            r"\b(?:need|want|looking for|help me (?:find|choose)|find me|recommend|suggest|show me)\b",
            message,
            re.I,
        )
        and not re.search(r"\b(?:advice|explain|discuss|definition|mean by)\b", message, re.I)
        and any(
            change.field == "need" and change.value and evidence_ok(change.evidence, message)
            for change in plan.changes
        )
    )
    if explicit_goal:
        plan.recommendation_requested = True
        if plan.intent == "clarify":
            plan.intent = "refine" if state.active() else "search"
    active = state.active()
    if not active or not active.shown:
        return
    refinable = plan.intent in {"refine", "search", "clarify"} and any(
        change.field not in {"domain", "family", "unsupported"}
        and evidence_ok(change.evidence, message)
        for change in plan.changes
    )
    if refinable or (plan.feedback == "dislike" and plan.intent in {"feedback", "refine", "more"}):
        plan.recommendation_requested = True
        if plan.intent in {"clarify", "feedback"}:
            plan.intent = "refine"


def retrieval_query(task, plan, message, storefront_id=None):
    """Keep hypotheses out of filters, identity gates and user preference evidence."""
    literal = " ".join(part for part in (task.query, task.need) if part)
    brief = {
        "goal": task.query,
        "need": task.need,
        "recipient": task.recipient,
        "filters": task.filters.model_dump(mode="json"),
    }
    brief["filters"]["storefront_id"] = storefront_id or task.filters.storefront_id
    interpretation = plan.interpretation
    if interpretation and evidence_ok(interpretation.evidence, message):
        task.sources["retrieval_interpretation"] = {
            "query": interpretation.query,
            "evidence": interpretation.evidence,
            "source": "semantic_retrieval_hypothesis",
            "requirements": brief,
        }
    saved = task.sources.get("retrieval_interpretation", {})
    # A saved rewrite is valid only for the exact brief it described.
    query = saved.get("query") if saved.get("requirements") == brief else None
    return query or literal


def discover_goal(state, task, plan, message):
    """Browse the selected section for an expressed goal without inventing a product."""
    if task.query or plan.recommendation_requested is not True or not task.need:
        return
    scope = state.storefront_id or FAMILY_STOREFRONT.get(task.filters.family)
    section = STOREFRONTS.get(scope)
    if not section:
        return
    task.query = {
        "clothing": "clothing",
        "beauty": "beauty and personal care",
        "footwear": "footwear",
        "fashion": "fashion accessories",
    }[scope]
    task.filters.domain = section["domain"]
    task.sources["query"] = {
        "value": task.query,
        "source": "goal_guided_discovery",
        "evidence": message,
        "specific_product_type_selected": False,
    }
