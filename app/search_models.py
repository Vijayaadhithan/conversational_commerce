"""Search models."""

from decimal import Decimal
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StringConstraints,
    field_validator,
    model_validator,
)

AttributeName = Literal[
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


AttributeValue = (
    StrictBool
    | Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
)


ProductID = Annotated[str, StringConstraints(pattern=r"^amazon:[A-Z0-9]{10}$")]


class ProductFilters(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    domain: Literal["beauty", "fashion"] | None = None
    family: str | None = Field(default=None, pattern=r"^[a-z][a-z_]{1,40}$")
    brand: str | None = Field(default=None, min_length=1, max_length=160)
    min_price: Decimal | None = Field(default=None, ge=0, max_digits=20, decimal_places=2)
    max_price: Decimal | None = Field(default=None, ge=0, max_digits=20, decimal_places=2)
    attributes: dict[AttributeName, AttributeValue] = Field(default_factory=dict)
    exclude_product_ids: list[ProductID] = Field(default_factory=list, max_length=100)
    exclude_brands: list[
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)]
    ] = Field(default_factory=list, max_length=10)

    @field_validator("brand", "family")
    @classmethod
    def valid_text(cls, value):
        if value is not None:
            try:
                value.encode("utf-8")
            except UnicodeError as exc:
                raise ValueError("Filter text must be valid Unicode") from exc
            if "\x00" in value:
                raise ValueError("Filter text cannot contain NUL")
        return value

    @model_validator(mode="after")
    def check_constraints(self):
        for brand in self.exclude_brands:
            self.valid_text(brand)
        if (
            self.min_price is not None
            and self.max_price is not None
            and self.min_price > self.max_price
        ):
            raise ValueError("min_price must not exceed max_price")
        for key, value in self.attributes.items():
            if (key == "fragrance_free") != isinstance(value, bool):
                raise ValueError(
                    "fragrance_free requires a boolean; other attribute filters require text"
                )
            if isinstance(value, str):
                self.valid_text(value)
        return self


class ProductSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(
        min_length=1,
        max_length=1000,
        description="Relevance text; mandatory constraints must also be provided in filters. Maximum 1,000 UTF-8 bytes.",
    )
    filters: ProductFilters = Field(
        default_factory=ProductFilters,
        description="Mandatory constraints applied to both retrieval paths; never relaxed.",
    )
    limit: int = Field(default=5, ge=1, le=20)
    rerank: StrictBool = Field(
        default=True,
        description="Rerank eligible candidates with the installed local model. False explicitly returns the RRF baseline.",
    )
    product_goal: str = Field(
        default="",
        max_length=1200,
        description="Explicit product type from the user; source wording gates retrieval before ranking.",
    )
    concerns: list[
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]
    ] = Field(
        default_factory=list,
        max_length=20,
        description="Soft concerns for separate source-backed explanations; not mandatory filters.",
    )

    @field_validator("query")
    @classmethod
    def validate_query(cls, value):
        try:
            size = len(value.encode("utf-8"))
        except UnicodeError as exc:
            raise ValueError("Query must be valid Unicode") from exc
        if not value.strip() or size > 1000 or "\x00" in value:
            raise ValueError("Query must contain text and fit within 1,000 UTF-8 bytes without NUL")
        return value
