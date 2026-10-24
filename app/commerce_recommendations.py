"""Recommendations for a single shopping task."""

import re
from decimal import Decimal

from app.commerce_discovery import question, skin_discovery
from app.commerce_errors import IntentError
from app.commerce_replies import card_reply, explain_products, task_concerns
from app.commerce_text import (
    negated,
    normalized,
    reference_positions,
)
from app.search_models import ProductFilters, ProductSearchRequest


def recommend_or_inspect(state, plan, message, old_pending, search):
    task = state.active()
    if plan.intent in {"details", "compare", "similar"}:
        positions = reference_positions(message, len(task.shown))
        if not positions and len(task.shown) == 1:
            positions = [1]
        literal_ids = re.findall(r"amazon:[A-Z0-9]{10}", message)
        if literal_ids:
            positions = [
                index + 1
                for index, row in enumerate(task.shown)
                if row["product_id"] in literal_ids
            ]
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
                "reply": note + card_reply(selected, compare=plan.intent == "compare"),
                "products": selected,
                "search": task.last_search,
            }
        if len(selected) != 1:
            return state, question(
                state, "Which single product should I use for similar options?", "reference"
            )
        query = selected[0]["title"]
    else:
        query = " ".join(part for part in (task.query, task.need) if part)
    if task.query and plan.recommendation_requested is False:
        return state, question(
            state,
            f"I've kept your shopping context for {task.query}. What would you like to clarify about your preferences?",
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
            "Who are these for (for example adult men, adult women, or children)? Please mention any required size and sizing system too; only explicit listing claims can be matched.",
            "recipient",
        )
    filters = task.filters.model_copy(deep=True)
    if plan.intent == "cheaper":
        if not task.shown:
            return state, question(
                state,
                "Which displayed product or price should the cheaper options be compared with?",
                "reference",
            )
        if not re.search(r"cheap|less expensive|lower (?:price|cost)", normalized(message)):
            raise IntentError("Relative budget change lacks evidence")
        reference = min(task.shown, key=lambda row: Decimal(row["price"]["amount"]))
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
        task.last_search = search_metadata
    if not products:
        reply = f"I found no {task.query} listings matching all your saved requirements (budget maximum: {'$' + str(task.filters.max_price) + ' USD' if task.filters.max_price is not None else 'not set'}). I kept those requirements unchanged. You can explicitly change a budget, brand, or attribute to try again."
    else:
        reply = (
            f"For your request: {task.query}"
            + (f"; concern: {task.need}" if task.need else "")
            + ".\n"
            + card_reply(products)
        )
        reply += "\nThese are historical listing claims, not verified outcomes or current stock. Tell me what to change, ask for more options, or refer to a product number."
    return state, {
        "action": "recommend",
        "reply": reply,
        "products": products,
        "search": search_metadata,
    }
