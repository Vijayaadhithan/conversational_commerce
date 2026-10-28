"""Structured intent proposals and persisted shopping state, independent of transcript summaries."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool

# Retain established imports while runtime consumers use the owning modules.
from app.commerce_prompts import PLANNER_PROMPT as PLANNER_PROMPT
from app.commerce_prompts import STOREFRONT_PROMPT as STOREFRONT_PROMPT
from app.commerce_prompts import planner_context as planner_context
from app.commerce_prompts import planner_examples as planner_examples
from app.commerce_taxonomy import FAMILIES as FAMILIES
from app.search_models import ProductFilters
from app.storefronts import StorefrontID

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
    scope: Literal["shopping", "smalltalk", "off_topic", "unclear"] | None = None
    requested_storefront: StorefrontID | None = None
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
        "answer",
        "feedback",
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
    feedback: Literal["like", "dislike"] | None = None
    cheaper_than_reference: StrictBool = False
    question_kind: Literal[
        "product_type", "skin_profile", "recipient", "reference", "multiple", "general"
    ] = "product_type"


class Concern(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str = Field(min_length=1, max_length=1000)
    evidence: str = Field(min_length=1, max_length=4000)


class ProductFeedback(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sentiment: Literal["like", "dislike"]
    evidence: str = Field(min_length=1, max_length=4000)
    product_ids: list[str] = Field(min_length=1, max_length=5)


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
    focused_positions: list[int] = Field(default_factory=list, max_length=5)
    feedback: list[ProductFeedback] = Field(default_factory=list, max_length=20)


class ShoppingState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    storefront_id: StorefrontID | None = None
    tasks: list[ShoppingTask] = Field(default_factory=list, max_length=5)
    active_task_id: int | None = None
    pending_question: str = ""
    pending_kind: str = ""
    turns: int = 0
    displayed_task_ids: list[int] = Field(default_factory=list, max_length=5)

    def active(self):
        return next((task for task in self.tasks if task.id == self.active_task_id), None)


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


def shopping_state_for(record: dict) -> ShoppingState:
    """Restore state with the immutable conversation scope as the authority."""
    state = ShoppingState.model_validate(record["shopping_state"])
    state.storefront_id = record.get("storefront_id")
    return state
