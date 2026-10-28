"""Recommendations for a single shopping task."""

import re
from decimal import Decimal

from app.commerce_discovery import (
    no_results_reply,
    preference_question,
    question,
    skin_discovery,
)
from app.commerce_errors import IntentError
from app.commerce_feedback import rejected_ids, resolved_positions
from app.commerce_replies import card_reply, explain_products, task_concerns
from app.commerce_text import (
    negated,
    normalized,
)
from app.search_models import ProductFilters, ProductSearchRequest


def recommend_or_inspect(state, plan, message, old_pending, search):
    task = state.active()
    if plan.intent in {"details", "compare", "similar", "answer"}:
        positions = resolved_positions(task, message)
        if (
            plan.intent == "answer"
            and not positions
            and not re.search(
                r"\b(?:it|this|that|one|first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|last|option\s+\d+|product\s+\d+)\b|amazon:",
                message,
                re.I,
            )
        ):
            positions = list(range(1, len(task.shown) + 1))
        if (
            not positions
            or any(not 1 <= position <= len(task.shown) for position in positions)
            or (plan.intent == "compare" and len(positions) < 2)
        ):
            return state, question(
                state,
                "Which product number(s) from the latest displayed list do you mean?",
                "reference",
            )
        selected = [task.shown[position - 1] for position in positions]
        task.focused_positions = positions
        if plan.intent != "similar":
            previous = ProductFilters.model_validate(task.last_search.get("filters_applied", {}))
            changed = previous.model_dump(
                exclude={"exclude_product_ids"}
            ) != task.filters.model_dump(exclude={"exclude_product_ids"})
            note = (
                "These are earlier displayed options, not confirmed to match your current requirements.\n"
                if changed
                else ""
            )
            return state, {
                "action": plan.intent,
                "reply": note
                + card_reply(
                    selected, compare=plan.intent == "compare", compact=plan.intent == "compare"
                ),
                "products": selected,
                "search": task.last_search,
                "snapshot_changed": changed,
            }
        if len(selected) != 1:
            return state, question(
                state, "Which single product should I use for similar options?", "reference"
            )
        query = selected[0]["title"]
    else:
        query = " ".join(part for part in (task.query, task.need) if part)
    if task.query and plan.recommendation_requested is False and plan.intent != "similar":
        return state, question(
            state,
            f"I've saved your preferences for {task.query}. {preference_question(task)}",
            "general",
        )
    if not task.query:
        guided = skin_discovery(state, task, message, old_pending, plan)
        if guided:
            return state, guided
        query = " ".join(part for part in (task.query, task.need) if part)
    if not task.query:
        examples = {
            "clothing": "a shirt, dress, or jacket",
            "beauty": "a moisturizer, shampoo, or lipstick",
            "footwear": "running shoes, sandals, or boots",
            "fashion": "a bag, watch, or necklace",
        }.get(state.storefront_id, "a moisturizer, cleanser, shirt, or bag")
        return state, question(
            state,
            f"What type of product are you looking for—for example {examples}?",
            "product_type",
        )
    if task.filters.family in {"footwear", "apparel"} and not any(
        name in task.filters.attributes for name in ("department", "age_range")
    ):
        return state, question(
            state,
            "Who are these for (for example adult men, adult women, or children)?",
            "recipient",
        )
    filters = task.filters.model_copy(deep=True)
    filters.exclude_product_ids = list(
        dict.fromkeys([*filters.exclude_product_ids, *rejected_ids(task)])
    )
    if plan.cheaper_than_reference and plan.intent != "similar":
        raise IntentError("A relative price modifier requires a similar-product request")
    if plan.intent == "cheaper" or plan.cheaper_than_reference:
        if not task.shown:
            return state, question(
                state,
                "Which displayed product or price should the cheaper options be compared with?",
                "reference",
            )
        if not re.search(r"cheap|less expensive|lower (?:price|cost)", normalized(message)):
            raise IntentError("Relative budget change lacks evidence")
        reference_rows = selected if plan.cheaper_than_reference else task.shown
        reference = min(reference_rows, key=lambda row: Decimal(row["price"]["amount"]))
        maximum = Decimal(reference["price"]["amount"]) - Decimal("0.01")
        if filters.max_price is not None:
            maximum = min(maximum, filters.max_price)
        if maximum < 0 or (filters.min_price is not None and maximum < filters.min_price):
            return state, question(
                state,
                "A cheaper maximum would conflict with your saved minimum price. Which price requirement should change?",
            )
        task.filters.max_price = maximum
        filters.max_price = maximum
        task.sources["max_price"] = {
            "value": str(maximum),
            "source": "computed_from_displayed_price",
            "product_id": reference["product_id"],
            "evidence": message,
        }
    if plan.intent == "different_brand":
        if not re.search(r"another brand|different brand|other brand", normalized(message)):
            raise IntentError("Brand exclusion was not requested")
        brands = {row["brand"]["value"] for row in task.shown if row.get("brand")}
        if not brands:
            return state, question(
                state,
                "The displayed listings have no explicit brand metadata. Which brand should I avoid?",
                "reference",
            )
        filters.brand = None
        task.filters.brand = None
        exclusions = sorted(set(filters.exclude_brands) | brands)
        if len(exclusions) > 10:
            return state, question(
                state,
                "There are already too many excluded brands. Explicitly clear brand preferences before excluding more.",
            )
        filters.exclude_brands = exclusions
        task.filters.exclude_brands = filters.exclude_brands
        task.sources["exclude_brands"] = {
            "value": filters.exclude_brands,
            "source": "explicit_other_brand_request",
            "evidence": message,
        }
    if plan.intent == "more":
        if len(task.seen_ids) >= 100:
            return state, question(
                state,
                "You have seen 100 products in this task. Start a new task or explicitly reset it to allow a fresh result set.",
            )
        filters.exclude_product_ids = list(
            dict.fromkeys([*filters.exclude_product_ids, *task.seen_ids])
        )
    if plan.intent == "similar":
        filters.exclude_product_ids = list(
            dict.fromkeys([*filters.exclude_product_ids, selected[0]["product_id"]])
        )
    if len(query.encode("utf-8")) > 1000:
        return state, question(
            state,
            "Your saved request is too long to search in one query. Which concerns should I remove or replace to focus this search?",
            "query_size",
        )
    product_goal = task.query
    if re.search(r"\bpockets?\b", task.need, re.I) and not negated("pockets", task.need):
        product_goal += " with pockets"
    request = ProductSearchRequest(
        query=query,
        product_goal=product_goal,
        concerns=task_concerns(task),
        filters=ProductFilters.model_validate(filters.model_dump()),
        limit=min(5, 100 - len(task.seen_ids)) if plan.intent == "more" else 5,
    )
    response = search(request)
    products = explain_products(response["results"], task)
    new_ids = [row["product_id"] for row in products if row["product_id"] not in task.seen_ids]
    if len(task.seen_ids) + len(new_ids) > 100:
        return state, question(
            state,
            "This task's 100-product history is full. Start a new task or explicitly reset it before showing more products.",
        )
    task.seen_ids.extend(new_ids)
    search_metadata = {key: value for key, value in response.items() if key != "results"}
    if products:
        task.shown = products
        task.focused_positions = []
        task.last_search = search_metadata
    if not products:
        reply = no_results_reply(task, more=plan.intent == "more")
    else:
        reply = f"Here are {len(products)} options for {task.query}:\n" + card_reply(
            products, compact=True
        )
        reply += "\nPrices are historical USD; suitability and stock are unverified. Pick a number to explore, or tell me what to change."
    return state, {
        "action": "recommend",
        "reply": reply,
        "products": products,
        "search": search_metadata,
    }
