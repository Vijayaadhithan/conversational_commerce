"""Route explicit requests and enforce conversation scope around the existing flow."""

from collections.abc import Callable

from app.catalog import CatalogError
from app.commerce_errors import IntentError, PreferenceConflict, ScopeConflict
from app.commerce_models import IntentPlan, ShoppingState
from app.commerce_requests import multi_requests
from app.commerce_text import evidence_ok, negated, product_mentions
from app.search_models import ProductSearchRequest
from app.search_normalization import normalize_search
from app.storefronts import FAMILY_STOREFRONT, STOREFRONTS, category_allowed, filters_allowed

Search = Callable[[ProductSearchRequest], dict]
TurnHandler = Callable[[dict, IntentPlan, str, Search], tuple[ShoppingState, dict]]


def clarification(state: ShoppingState, message: str) -> tuple[ShoppingState, dict]:
    # Do not overwrite the active task's pending question or any saved preferences.
    return state, {"action": "clarify", "reply": message, "products": [], "search": None}


def request_sections(text: str) -> list[str]:
    return list(
        dict.fromkeys(
            FAMILY_STOREFRONT[family]
            for _, _, family, term in product_mentions(text)
            if not negated(term, text)
        )
    )


def route_request(
    state: ShoppingState, plan: IntentPlan, message: str
) -> tuple[ShoppingState, dict] | None:
    """A handoff is an offer to open a scoped request, never a state transfer."""
    if not evidence_ok(plan.evidence, message):
        raise IntentError("Intent lacks current-message evidence")
    if plan.intent in {
        "details",
        "compare",
        "more",
        "cheaper",
        "different_brand",
        "similar",
        "reset",
        "preferences",
        "smalltalk",
    }:
        return None
    if plan.recommendation_requested is False and plan.requested_storefront is None:
        return None
    try:
        requests = multi_requests(plan, message)
    except PreferenceConflict as exc:
        return clarification(state, str(exc))
    clauses = [request.evidence for request in requests] or [message]
    targets = []
    for clause in clauses:
        sections = request_sections(clause)
        if len(sections) > 1:
            return clarification(
                state,
                "Which item would you like first? Please give each product its own request and budget.",
            )
        if sections:
            targets.append({"storefront_id": sections[0], "message": clause})
    # Semantic routing is a proposal, shown visibly for the user to choose.
    if not targets and plan.requested_storefront:
        targets.append({"storefront_id": plan.requested_storefront, "message": message})
    if not any(target["storefront_id"] != state.storefront_id for target in targets):
        return None
    for target in targets:
        section = STOREFRONTS[target["storefront_id"]]
        target.update(label=section["label"], href="/" + target["storefront_id"])
    return state, {
        "action": "handoff",
        "products": [],
        "search": None,
        "reply": (
            "These items belong in separate shopping sections. Open each request below; each section keeps its own requirements."
            if len(targets) > 1
            else f"Let's look for that in {targets[0]['label']}. Continue below with this request; your saved requirements here will stay in this section."
        ),
        "handoff": {"targets": targets},
    }


def handle_storefront_turn(
    saved: dict, plan: IntentPlan, message: str, search: Search, handler: TurnHandler
) -> tuple[ShoppingState, dict]:
    state = ShoppingState.model_validate(saved)
    scope = state.storefront_id
    if scope is None:
        return handler(saved, plan, message, search)
    if any(
        not filters_allowed(task.filters, scope)
        or any(not category_allowed(row.get("category"), scope) for row in task.shown)
        for task in state.tasks
    ):
        raise CatalogError("Saved shopping state does not match its storefront")
    routed = route_request(state, plan, message)
    if routed is not None:
        return routed

    def scoped_search(request):
        if not filters_allowed(request.filters, scope):
            raise ScopeConflict()
        payload = request.model_dump(mode="json")
        payload["filters"]["storefront_id"] = scope
        payload["query"] = normalize_search(request.query)
        payload["product_goal"] = normalize_search(request.product_goal)
        result = search(ProductSearchRequest.model_validate(payload))
        if any(not category_allowed(row.get("category"), scope) for row in result["results"]):
            raise CatalogError("Product search returned a different storefront")
        # Saved literal concerns remain the basis of evidence, even with search aliases.
        return dict(result, original_query=request.query, normalized_query=payload["query"])

    try:
        next_state, result = handler(saved, plan, message, scoped_search)
        for task in next_state.tasks:
            if not filters_allowed(task.filters, scope):
                raise ScopeConflict()
            task.filters.storefront_id = scope
        next_state.storefront_id = scope
        return next_state, result
    except ScopeConflict:
        return clarification(
            state,
            f"You're shopping in {STOREFRONTS[scope]['label']}. Which item in this section would you like, or would you like to open another section?",
        )
