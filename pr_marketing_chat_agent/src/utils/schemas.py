"""Structured LLM outputs for drafting, fact-checking and visual copy."""

from pydantic import BaseModel, Field


class GeneratedDraft(BaseModel):
    title: str | None = None
    content: str


class UnsupportedClaim(BaseModel):
    claim: str
    reason: str


class Verification(BaseModel):
    all_claims_supported: bool
    unsupported_claims: list[UnsupportedClaim] = Field(default_factory=list)
    revised_content: str | None = Field(
        default=None, description="Draft with unsupported claims removed; null if none"
    )


class SlotCopy(BaseModel):
    slots: dict[str, str] = Field(description="Slot name -> finished copy for that slot")
