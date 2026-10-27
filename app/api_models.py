"""Validated HTTP request bodies."""

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.storefronts import StorefrontID


class NewConversation(BaseModel):
    title: str = Field(default="New conversation", min_length=1, max_length=120)
    mode: Literal["chat", "commerce"] | None = None
    storefront_id: StorefrontID | None = None

    @model_validator(mode="after")
    def commerce_storefront(self):
        if self.storefront_id and self.mode == "chat":
            raise ValueError("A storefront requires commerce mode")
        return self


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)

    @field_validator("message")
    @classmethod
    def validate_message(cls, value):
        if not value.strip():
            raise ValueError("Message cannot be blank")
        if len(value.encode("utf-8")) > 4000:
            raise ValueError("Message must be at most 4000 UTF-8 bytes")
        return value


class RerankRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    documents: list[str] = Field(min_length=1, max_length=50)
    top_k: int = Field(default=10, ge=1, le=50)

    @field_validator("query")
    @classmethod
    def validate_query(cls, value):
        if not value.strip():
            raise ValueError("Query cannot be blank")
        return value

    @field_validator("documents")
    @classmethod
    def validate_documents(cls, values):
        if any(not value.strip() or len(value.encode("utf-8")) > 4000 for value in values):
            raise ValueError("Documents must be nonblank and at most 4000 UTF-8 bytes each")
        return values
