"""Typed schemas: every boundary (API, LLM, integrations) validates against these."""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field, field_validator


class Severity(str, Enum):
    critical = "Critical"
    high = "High"
    medium = "Medium"
    low = "Low"

    # Enum.__str__ shadows str.__str__ on a (str, Enum) mixin, so f"{Severity.high}"
    # renders "Severity.high" instead of "High" — override to get the plain value back.
    def __str__(self) -> str:
        return self.value


class Category(str, Enum):
    infrastructure = "Infrastructure"
    app_error = "Application Error"
    configuration = "Configuration"
    security = "Security"
    performance = "Performance"
    dependency = "Dependency/Network"
    data = "Data/DB"
    unknown = "Unknown"

    def __str__(self) -> str:
        return self.value


def _normalize_risk(v: object) -> object:
    # LLMs often echo the Title Case used elsewhere in the app ("Medium") even when
    # asked for lowercase; normalize before the low|medium|high pattern check runs.
    return v.strip().lower() if isinstance(v, str) else v


class EvidenceRef(BaseModel):
    file: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)


class Issue(BaseModel):
    fingerprint: str
    category: Category
    severity: Severity
    confidence: float = Field(ge=0.0, le=1.0)
    summary: str
    count: int = 1
    first_seen: Optional[str] = None
    last_seen: Optional[str] = None
    evidence: list[EvidenceRef] = []
    unparsed: bool = False


class Remediation(BaseModel):
    fingerprint: str
    root_cause: str
    fix_steps_md: str
    rationale: str
    risk: str = Field(pattern="^(low|medium|high)$")
    destructive: bool = False
    citations: list[str] = []
    rank: int = 1

    _normalize_risk = field_validator("risk", mode="before")(_normalize_risk)


# --- LLM raw output shapes (validated then normalized) ---
class ClassifierIssueOut(BaseModel):
    category: Category
    severity: Severity
    confidence: float = Field(ge=0.0, le=1.0)
    summary: str
    error_signature: str = Field(description="normalized error text used for fingerprinting")
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    timestamp_first: Optional[str] = None
    timestamp_last: Optional[str] = None
    occurrences: int = Field(default=1, ge=1)


class ClassifierOut(BaseModel):
    issues: list[ClassifierIssueOut] = []


class RemediationOut(BaseModel):
    root_cause: str
    fix_steps_md: str
    rationale: str
    risk: str = Field(pattern="^(low|medium|high)$")
    destructive: bool = False
    citations: list[str] = []

    _normalize_risk = field_validator("risk", mode="before")(_normalize_risk)


class CookbookOut(BaseModel):
    content_md: str


# --- API shapes ---
class NodeEvent(BaseModel):
    run_id: str
    node: str
    status: str  # pending|running|done|failed|skipped|waiting_approval
    detail: str = ""
    ts: datetime = Field(default_factory=datetime.utcnow)


class RunStatus(BaseModel):
    id: str
    status: str
    nodes: dict[str, str]
    issues: list[Issue] = []
    remediations: list[Remediation] = []
    cookbook_md: Optional[str] = None
    slack: Optional[dict] = None
    jira: list[dict] = []
    errors: list[str] = []
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    filename: str = ""
    created_at: Optional[str] = None


class ApproveAction(BaseModel):
    approve: bool = True
