from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class HandoverStatus(StrEnum):
    draft = "draft"
    needs_review = "needs_review"
    approved = "approved"


class EvaluationLabel(StrEnum):
    pass_ = "PASS"
    retrain = "RETRAIN"
    incomplete = "INCOMPLETE"


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_id: str
    document_id: str
    document_version: int = Field(ge=1)
    ingestion_id: str
    chunk_id: str
    block_ids: list[str] = Field(min_length=1)
    pdf_page_number: int = Field(ge=1)


class EvidenceText(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)


class ProcedureStep(EvidenceText):
    order: int = Field(ge=1)


class HandoverTask(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    overview: EvidenceText
    prerequisites: list[EvidenceText]
    steps: list[ProcedureStep]
    cautions: list[EvidenceText]
    troubleshooting: list[EvidenceText]


class HandoverDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1.0"
    handover_id: str
    revision: int = Field(ge=1)
    title: str = Field(min_length=1)
    status: HandoverStatus
    owner_employee_id: str | None
    recipient_employee_id: str | None
    tasks: list[HandoverTask]
    evidence: list[Evidence]
    missing_fields: list[str]
    conflicts: list[str]
    generation_run_id: str
    template_version: str

    @model_validator(mode="after")
    def validate_evidence_references(self):
        evidence_ids = {item.evidence_id for item in self.evidence}
        referenced_ids: set[str] = set()

        for task in self.tasks:
            referenced_ids.update(task.overview.evidence_ids)
            for item in task.prerequisites + task.steps + task.cautions + task.troubleshooting:
                referenced_ids.update(item.evidence_ids)

        unknown_ids = sorted(referenced_ids - evidence_ids)
        if unknown_ids:
            raise ValueError(f"등록되지 않은 근거 ID입니다: {', '.join(unknown_ids)}")
        return self


class CriterionScore(BaseModel):
    model_config = ConfigDict(extra="forbid")

    criterion_id: str
    criterion: str
    score: float = Field(ge=0)
    max_score: float = Field(gt=0)
    answer_evidence: str | None = None
    source_evidence_ids: list[str]
    confidence: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def validate_score_range(self):
        if self.score > self.max_score:
            raise ValueError("기준 점수는 만점을 초과할 수 없습니다.")
        return self


class EvaluationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    python_score: float | None = Field(default=None, ge=0, le=100)
    llm_score: float | None = Field(default=None, ge=0, le=100)
    hybrid_score: float | None = Field(default=None, ge=0, le=100)
    difference: float | None = Field(default=None, ge=0, le=100)
    label: EvaluationLabel | None
    status: str
    needs_review: bool
    reason: str | None = None
    scoring_policy_version: str = "1.0"
