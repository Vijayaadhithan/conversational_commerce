"""Structured intent proposals and persisted shopping state, independent of transcript summaries."""

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool

from app.search_models import ProductFilters

FAMILIES = {
    "skin_care": (
        "beauty",
        [
            "moisturi",
            "cleanser",
            "serum",
            "lotion",
            "sunscreen",
            "skin care",
            "skincare",
            "face cream",
        ],
    ),
    "hair_care": ("beauty", ["shampoo", "conditioner", "hair oil", "hair care"]),
    "makeup": ("beauty", ["lipstick", "makeup", "mascara", "foundation", "eyeliner", "lip gloss"]),
    "fragrance": ("beauty", ["perfume", "fragrance spray", "cologne"]),
    "oral_care": ("beauty", ["toothpaste", "toothbrush", "mouthwash", "floss"]),
    "shaving": ("beauty", ["razor blade", "razor", "shaving", "shaver"]),
    "beauty_tools": ("beauty", ["makeup brush", "beauty tool", "nail clipper"]),
    "personal_care": ("beauty", ["deodorant", "body wash", "soap", "personal care"]),
    "footwear": ("fashion", ["shoe", "sneaker", "boot", "sandal", "slipper", "footwear"]),
    "apparel": (
        "fashion",
        ["shirt", "dress", "jeans", "trouser", "jacket", "blouse", "clothing", "apparel", "pants"],
    ),
    "bags": ("fashion", ["handbag", "crossbody", "bag", "purse", "backpack", "wallet"]),
    "jewelry": ("fashion", ["necklace", "earring", "bracelet", "jewelry", "jewellery", "ring"]),
    "watches": ("fashion", ["watch"]),
    "accessories": ("fashion", ["scarf", "belt", "hat", "sunglasses", "accessor"]),
}
ATTRIBUTES = [
    "color",
    "size",
    "material",
    "department",
    "age_range",
    "skin_type",
    "hair_type",
    "ingredients",
    "active_ingredients",
    "benefits",
    "material_type_free",
    "scent",
    "item_form",
    "unit_count",
    "fragrance_free",
    "product_type",
    "volume",
    "style",
]
FieldName = Literal[
    "query",
    "need",
    "recipient",
    "domain",
    "family",
    "brand",
    "min_price",
    "max_price",
    "unsupported",
    "attribute.color",
    "attribute.size",
    "attribute.material",
    "attribute.department",
    "attribute.age_range",
    "attribute.skin_type",
    "attribute.hair_type",
    "attribute.ingredients",
    "attribute.active_ingredients",
    "attribute.benefits",
    "attribute.material_type_free",
    "attribute.scent",
    "attribute.item_form",
    "attribute.unit_count",
    "attribute.fragrance_free",
    "attribute.product_type",
    "attribute.volume",
    "attribute.style",
]


class Change(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field: FieldName
    value: str | StrictBool | None
    evidence: str = Field(min_length=1, max_length=500)


class TaskRequest(BaseModel):
    """One verbatim, independently scoped part of a multi-product message."""

    model_config = ConfigDict(extra="forbid")
    evidence: str = Field(min_length=1, max_length=1000)
    changes: list[Change] = Field(default_factory=list, max_length=20)


class IntentPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    intent: Literal[
        "search",
        "refine",
        "more",
        "cheaper",
        "different_brand",
        "details",
        "compare",
        "similar",
        "clarify",
        "new_task",
        "switch_task",
        "reset",
        "preferences",
        "smalltalk",
    ]
    evidence: str = Field(min_length=1, max_length=500)
    recommendation_requested: StrictBool | None = Field(
        default=None,
        description="True when the user wants product suggestions now, based on the latest message and saved conversation context; false for discussion, deferral, or refusal. Null is for legacy plans only.",
    )
    changes: list[Change] = Field(default_factory=list, max_length=20)
    requests: list[TaskRequest] = Field(default_factory=list, max_length=5)
    positions: list[int] = Field(default_factory=list, max_length=5)
    task_id: int | None = Field(default=None, ge=1, le=5)
    question_kind: Literal[
        "product_type", "skin_profile", "recipient", "reference", "multiple", "general"
    ] = "product_type"


class Concern(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str = Field(min_length=1, max_length=1000)
    evidence: str = Field(min_length=1, max_length=4000)


class ShoppingTask(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    query: str = ""
    need: str = ""
    needs: list[Concern] = Field(default_factory=list, max_length=20)
    recipient: str = ""
    filters: ProductFilters = Field(default_factory=ProductFilters)
    unsupported: list[str] = Field(default_factory=list, max_length=10)
    sources: dict[str, dict] = Field(default_factory=dict)
    shown: list[dict] = Field(default_factory=list, max_length=5)
    seen_ids: list[str] = Field(default_factory=list, max_length=100)
    last_search: dict = Field(default_factory=dict)
    pending_question: str = ""
    pending_kind: str = ""


class ShoppingState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    tasks: list[ShoppingTask] = Field(default_factory=list, max_length=5)
    active_task_id: int | None = None
    pending_question: str = ""
    pending_kind: str = ""
    turns: int = 0
    displayed_task_ids: list[int] = Field(default_factory=list, max_length=5)

    def active(self):
        return next((task for task in self.tasks if task.id == self.active_task_id), None)


PLANNER_PROMPT = """You extract shopping intent for a continuous fashion/beauty conversation. Return only the IntentPlan JSON.
The saved state is authoritative. Extract ONLY changes explicitly supported by the LATEST MESSAGE, never restate old fields.
Always decide recommendation_requested as true or false by MEANING, using the latest message, saved concerns, and pending question.
True means the user wants product suggestions now: 'give me soem products with the above conditions', 'what would you pick for me?',
'anything matching that?', 'go ahead' after an offer to browse, and natural requests with typos all count. No special phrase is required.
Use intent=search or refine for those requests. Reference the saved conditions without copying them into new changes.
When browsing from saved concerns without naming new product types, return changes=[] and requests=[].
requests describes product types the USER explicitly named in this message, never products you think they should buy.
Updating concrete budget/brand/color/size requirements for an existing product requests refreshed recommendations unless explicitly deferred.
For example 'shampoo under $10 instead' means refine the saved shampoo task AND recommendation_requested=true.
Naming a product/task in answer to a task-selection question also resumes recommendations for that task.
False means the user is only providing context, asking about a preference, or deferring/refusing products (including messages that
mention 'show options' in a question or quotation). For example 'I have dry skin' and 'do not suggest products yet' are false.
Answering a pending skin-profile question can continue discovery; a new concern or vague preference alone can first need clarification.
Missing optional skin type, budget, or a specific cream/serum choice must not block a request to browse an already-known skin concern.
This decision never authorizes removing saved filters or treating vague preferences as verified product properties.
Every change needs an exact short quote from the latest message as evidence. Null means an explicitly requested removal;
omitted fields remain unchanged. Never clear a budget/brand/attribute just to find matches. Never invent product IDs or prices.
Use query for the product type/goal, need for an explicitly mentioned concern (e.g. dry skin), recipient for who it is for.
Text preference values must be literal short excerpts from the latest message, not rewritten or expanded descriptions.
Do not invent a product type from a concern alone: 'I have dry skin' needs clarification. 'a moisturizer' supplies the type.
Always save an explicitly stated concern as a need change, even when asking for the product type. A concern alone is
question_kind=skin_profile for facial/skin concerns, otherwise product_type, never multiple. Multiple means the user explicitly
requested two different product types, not several concerns. Several need changes are allowed in one turn and accumulate.
Keep the user's spelling in need values and evidence, including typos; never silently rewrite 'pimble' to 'pimples'.
For skin concerns, ask about skin type/budget rather than repeatedly demanding a named product type. The application can
search broad skin-care listings after a skin-profile answer or recommendation_requested=true. Never invent a serum/cream.
Skin type supplied as personal context is a need; attribute.skin_type is a mandatory filter only when explicitly required.
Use search for a first product request, refine for budget/color/brand/need changes. Keep the old goal for 'under $20 instead'.
Use new_task when the user starts a different product/category; switch_task with a known task_id to return to a saved task.
New tasks start fresh; switching tasks restores their exact saved constraints. For multiple product types, use requests:
each entry has evidence (the complete verbatim clause for ONE product, including its requirements) and changes for that clause.
For a single product use top-level changes and requests=[]. Never wrap a single product in requests.
Leave top-level changes empty when using requests. Include every clause, in order, without overlapping evidence.
For example 'shampoo under $15 and moisturizer under $25' has two requests with separate queries and budgets.
Two products in the same family (cleanser and moisturizer) also need separate requests. Never transfer a requirement
between products. If a shared budget/recipient/attribute has ambiguous scope, use clarify/question_kind=multiple.
Domain and family are application-owned mappings: omit these changes when a named product supplies the category.
Known families are supplied in the context. On follow-ups, name the saved product in query only when actually stated.
Vague preferences such as 'less chemical', 'gentler', or 'not too strong' are literal need text, never inferred
fragrance_free, organic, ingredient exclusions, or safety claims. Keep spelling errors in the evidence and value.
min_price/max_price values are decimal strings; quote the amount. Currency is historical USD only. Do not convert currencies.
Attribute fields use attribute.NAME. Attribute text must be explicit in the quote; department may map men->mens, women->womens,
boys->boys, girls->girls. Fragrance-free must be boolean true ONLY for an explicit fragrance-free requirement; unscented is different.
For a required size with an explicit US/UK/EU sizing system, include the system in both the size value and its evidence.
Volume is the explicitly requested amount of product (e.g. 100 ml); never turn bottle volume into clothing size.
Product type, volume and style filters require explicit user requirements. Listed size/color options do not establish stock.
Record unsupported requirements (current stock/delivery, verified allergy or medical safety, non-USD budgets) as field unsupported,
value equal to the relevant user quote. Do not claim that listing metadata verifies those requirements.
Every explicitly stated requirement must be saved in a supported field or marked unsupported; never quietly discard one.
Use more for additional unseen options, cheaper for cheaper than shown, different_brand for another brand.
Use details/compare/similar with 1-based positions in the most recent displayed product list. Ask reference clarification if ambiguous.
Use reset ONLY when explicitly asked to start over, preferences to explain saved requirements, smalltalk for greetings/thanks.
If asked to remove a constraint use a null change with the explicit removal quote. Do not emit inherited or inferred changes.
"""


def preference_view(state):
    value = state.model_dump(mode="json", exclude={"tasks"})
    value["tasks"] = [
        dict(
            task.model_dump(mode="json", exclude={"shown", "last_search", "seen_ids"}),
            seen_count=len(task.seen_ids),
        )
        for task in state.tasks
    ]
    return value


def planner_context(state, message):
    tasks = []
    for task in state.tasks:
        item = {"id": task.id, "query": task.query, "need": task.need, "recipient": task.recipient}
        if task.id == state.active_task_id:
            item.update(
                filters=task.filters.model_dump(mode="json"),
                unsupported=task.unsupported,
                last_products=[
                    {"position": i + 1, "title": row["title"][:160], "brand": row.get("brand")}
                    for i, row in enumerate(task.shown)
                ],
            )
        tasks.append(item)
    return json.dumps(
        {
            "active_task_id": state.active_task_id,
            "tasks": tasks,
            "pending_question": state.pending_question,
            "pending_kind": state.pending_kind,
            "displayed_task_ids": state.displayed_task_ids,
            "families": list(FAMILIES),
            "latest_message": message,
        },
        ensure_ascii=False,
    )


def planner_examples():
    state = ShoppingState(
        tasks=[
            ShoppingTask(
                id=1,
                query="moisturizer",
                need="dry skin",
                filters=ProductFilters(domain="beauty", family="skin_care", max_price="29.99"),
            )
        ],
        active_task_id=1,
    )
    discovery = ShoppingState(
        tasks=[ShoppingTask(id=1, need="lightens my face; kess chemincal")],
        active_task_id=1,
        pending_kind="preference_detail",
        pending_question="Which ingredient do you want to avoid? You can also browse related listings.",
    )
    examples = [
        (
            ShoppingState(),
            "I have dry skin",
            {
                "intent": "clarify",
                "evidence": "I have dry skin",
                "changes": [{"field": "need", "value": "dry skin", "evidence": "dry skin"}],
                "question_kind": "product_type",
            },
        ),
        (
            ShoppingState(),
            "A moisturizer under $30",
            {
                "intent": "search",
                "evidence": "A moisturizer under $30",
                "changes": [
                    {"field": "query", "value": "moisturizer", "evidence": "moisturizer"},
                    {"field": "max_price", "value": "30", "evidence": "under $30"},
                ],
            },
        ),
        (
            state,
            "Under $20 instead",
            {
                "intent": "refine",
                "evidence": "Under $20 instead",
                "changes": [{"field": "max_price", "value": "20", "evidence": "Under $20"}],
            },
        ),
        (
            state,
            "Now I also need a shirt",
            {
                "intent": "new_task",
                "evidence": "Now I also need a shirt",
                "changes": [{"field": "query", "value": "shirt", "evidence": "shirt"}],
            },
        ),
        (
            ShoppingState(),
            "Something to brighten my face and reduce pimples",
            {
                "intent": "clarify",
                "evidence": "Something to brighten my face and reduce pimples",
                "question_kind": "skin_profile",
                "changes": [
                    {"field": "need", "value": "brighten my face", "evidence": "brighten my face"},
                    {"field": "need", "value": "reduce pimples", "evidence": "reduce pimples"},
                ],
            },
        ),
        (
            state,
            "something that is kess chemincal",
            {
                "intent": "refine",
                "evidence": "something that is kess chemincal",
                "changes": [
                    {"field": "need", "value": "kess chemincal", "evidence": "kess chemincal"}
                ],
            },
        ),
        (
            ShoppingState(),
            "shampoo under $15 and moisturizer under $25",
            {
                "intent": "search",
                "evidence": "shampoo under $15 and moisturizer under $25",
                "changes": [],
                "requests": [
                    {
                        "evidence": "shampoo under $15",
                        "changes": [{"field": "query", "value": "shampoo", "evidence": "shampoo"}],
                    },
                    {
                        "evidence": "moisturizer under $25",
                        "changes": [
                            {"field": "query", "value": "moisturizer", "evidence": "moisturizer"}
                        ],
                    },
                ],
            },
        ),
        (
            discovery,
            "give me soem products with the above conditions",
            {
                "intent": "search",
                "evidence": "give me soem products with the above conditions",
                "recommendation_requested": True,
                "changes": [],
            },
        ),
        (
            discovery,
            "Don't suggest anything yet; what do you mean by fragrance-free?",
            {
                "intent": "clarify",
                "evidence": "Don't suggest anything yet; what do you mean by fragrance-free?",
                "recommendation_requested": False,
                "question_kind": "general",
                "changes": [],
            },
        ),
    ]
    messages = []
    for saved, message, answer in examples:
        answer.setdefault(
            "recommendation_requested",
            answer["intent"] in {"search", "new_task"} or message == "Under $20 instead",
        )
        messages.extend(
            [
                {"role": "user", "content": planner_context(saved, message)},
                {"role": "assistant", "content": json.dumps(answer)},
            ]
        )
    return messages
