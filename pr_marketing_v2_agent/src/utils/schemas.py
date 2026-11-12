"""What the catalog updater's LLM steps must return. Every answer is validated against these
models; anything else is rejected before code acts on it."""

from typing import Literal

from pydantic import BaseModel, Field

UpdateKind = Literal["added", "changed", "fixed", "deprecated", "removed"]
Status = Literal["planned", "in_progress", "shipped", "deprecated"]
OpName = Literal[
    "add_update", "edit_field", "set_status", "upsert_component", "new_feature", "no_change"
]


class RouteResult(BaseModel):
    product_change: bool = Field(
        description="True if the event changes something a user of the product could notice, "
        "use or should know about. False for internal-only work (refactors, tests, CI, "
        "dependency bumps, internal tooling)."
    )
    features: list[str] = Field(
        default_factory=list, description="Slugs of existing features it touches, best first."
    )
    components: list[str] = Field(
        default_factory=list, description="Slugs of architecture components it touches."
    )
    new_feature: str | None = Field(
        default=None,
        description="Name of a new capability no existing feature covers, if the event "
        "introduces one.",
    )
    reason: str = ""


class Claim(BaseModel):
    id: str = Field(description="c1, c2, ...")
    statement: str = Field(description="One fact about the product, plain language.")
    evidence_id: str = Field(description="The evidence item the quote comes from, e.g. e1.")
    quote: str = Field(description="Exact words copied from that evidence item.")
    about: str = Field(
        description="Existing feature slug, 'component:<slug>', or 'new:<feature name>'."
    )


class ExtractResult(BaseModel):
    claims: list[Claim] = Field(default_factory=list)
    gaps: list[str] = Field(
        default_factory=list,
        description="Product changes the evidence mentions without enough detail to state.",
    )


class ClaimVerdict(BaseModel):
    claim_id: str
    supported: bool
    reason: str = ""


class VerifyResult(BaseModel):
    verdicts: list[ClaimVerdict] = Field(default_factory=list)


class Op(BaseModel):
    op: OpName
    target_type: Literal["feature", "component"] = "feature"
    target: str = Field(
        default="",
        description="Feature slug or component slug. For new_feature, leave empty.",
    )
    claim_ids: list[str] = Field(default_factory=list)
    kind: UpdateKind | None = Field(default=None, description="add_update only.")
    summary: str | None = Field(
        default=None, description="add_update: the update line. new_feature: one-line summary."
    )
    field: str | None = Field(
        default=None,
        description="edit_field only: summary, aliases, or overview.<what_it_is|who_its_for|"
        "how_it_works|limitations>.",
    )
    text: str | None = Field(
        default=None,
        description="edit_field only: the complete new value (aliases: comma-separated).",
    )
    status: Status | None = Field(default=None, description="set_status only.")
    name: str | None = Field(default=None, description="new_feature / upsert_component.")
    role: str | None = Field(default=None, description="upsert_component only.")
    depends_on: list[str] | None = Field(default=None, description="upsert_component only.")
    overview: dict[str, str] | None = Field(default=None, description="new_feature only.")
    components: list[str] | None = Field(default=None, description="new_feature only.")
    aliases: list[str] | None = Field(default=None, description="new_feature only.")
    reason: str = ""


class ComposeResult(BaseModel):
    ops: list[Op] = Field(default_factory=list)


class OpCheck(BaseModel):
    index: int
    verdict: Literal["supported", "narrowed", "unsupported"]
    revised_text: str | None = Field(
        default=None, description="For 'narrowed': the smallest edit that is fully supported."
    )
    reason: str = ""


class CheckResult(BaseModel):
    checks: list[OpCheck] = Field(default_factory=list)
