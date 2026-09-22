from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal, TypedDict
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class IncidentInput(StrictModel):
    source_event_id: str = Field(min_length=1, max_length=200)
    external_incident_id: str = Field(min_length=1, max_length=200)
    incident_id: UUID | None = None
    summary: str = Field(min_length=1, max_length=4000)
    service: str = Field(min_length=1, max_length=100)
    severity: Literal["critical", "high", "medium", "low"] = "medium"


class Envelope(StrictModel):
    schema_version: Literal[1] = 1
    event_id: UUID = Field(default_factory=uuid4)
    incident_id: UUID
    correlation_id: UUID
    event_type: str
    source: str
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    payload: dict[str, Any]


class Evidence(StrictModel):
    evidence_id: str
    tool: str
    observation: str
    observed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    trusted: bool = False
    simulated: bool = False


class Triage(StrictModel):
    severity: Literal["critical", "high", "medium", "low"]
    investigate: bool
    reason: str


class RCA(StrictModel):
    hypothesis: str
    confidence: float = Field(ge=0, le=1)
    evidence_ids: list[str]


class InvestigationStep(StrictModel):
    hypothesis: str
    tool: (
        Literal[
            "logs",
            "metrics",
            "kubernetes",
            "deployments",
            "config_diffs",
            "topology",
            "runbooks",
            "similar_incidents",
        ]
        | None
    ) = None
    query: str = Field(default="", max_length=1000)
    conclusion: RCA | None = None


class Action(StrEnum):
    OBSERVE = "observe"
    RESTART = "restart_workload"
    ROLLBACK = "rollback_deployment"
    SHELL = "shell"


class PlanDraft(StrictModel):
    action: Action
    target: str
    rationale: str
    rollback: str
    verification: str


class Plan(PlanDraft):
    plan_id: str


class Approval(StrictModel):
    plan_id: str
    approved: bool = Field(strict=True)


class IncidentState(TypedDict, total=False):
    input_event_id: str
    incident_id: str
    correlation_id: str
    raw: dict[str, Any]
    incident: dict[str, Any]
    triage: dict[str, Any]
    evidence: list[dict[str, Any]]
    hypotheses: list[str]
    rounds: int
    rca: dict[str, Any]
    rca_valid: bool
    plan: dict[str, Any]
    decision: str
    approval: dict[str, Any]
    execution: dict[str, Any]
    verified: bool
    status: str
