"""Structured LLM outputs and the deterministic post-worthiness verdict."""

from typing import Literal

from pydantic import BaseModel, Field

Category = Literal[
    "new_feature",
    "improvement",
    "performance",
    "bug_fix",
    "security_fix",
    "integration",
    "ui_change",
    "docs",
    "refactor",
    "tests",
    "ci_build",
    "dependencies",
    "internal_tooling",
    "other",
]

Decision = Literal["post_worthy", "release_notes_only", "hold", "internal"]

POST_WORTHY_MIN = 70
RELEASE_NOTES_MIN = 40


class Triage(BaseModel):
    category: Category
    user_facing: bool = Field(description="Would an end customer notice this change?")
    behind_feature_flag: bool = Field(description="Change is gated / not yet released to users")
    score: int = Field(ge=0, le=100, description="Marketing relevance, 0-100")
    reasons: list[str] = Field(description="Short evidence-based reasons for the score")
    recommended_formats: list[str] = Field(default_factory=list)


class Fact(BaseModel):
    fact: str
    source: str = Field(description="Where it is shown: PR title/description, file path, commit")


class Benefit(BaseModel):
    benefit: str
    evidence: str = Field(description="The verified fact this benefit rests on")


class Analysis(BaseModel):
    headline: str = Field(description="One plain-English line a customer would understand")
    plain_english: str = Field(description="2-4 sentence non-technical summary")
    what_changed: list[str]
    why_it_changed: str | None = None
    who_benefits: str | None = None
    customer_benefits: list[Benefit] = Field(default_factory=list)
    verified_facts: list[Fact]
    assumptions: list[str] = Field(default_factory=list)
    missing_info: list[str] = Field(default_factory=list)
    questions_for_dev: list[str] = Field(default_factory=list)


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


def decide(triage: Triage, *, merged: bool, draft: bool) -> Decision:
    """Turn triage signals into a verdict; code decides, not the model."""
    if draft or not merged or triage.behind_feature_flag:
        return "hold"
    if triage.score >= POST_WORTHY_MIN and triage.user_facing:
        return "post_worthy"
    if triage.score >= RELEASE_NOTES_MIN:
        return "release_notes_only"
    return "internal"
