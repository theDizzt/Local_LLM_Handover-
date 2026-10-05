# [읽기 안내] LLM/API 데이터가 지켜야 할 형식과 불변 조건을 정의한다.
# BaseModel 생성 시 Field 제약과 model_validator가 실행된다. DB 조회는 하지 않는다.
# '형식이 유효함'과 '실제 원문 근거가 존재함'은 별개이며 후자는 저장소 검증이 필요하다.
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class HandoverStatus(StrEnum):
    # StrEnum은 허용 문자열을 제한하면서 JSON에는 문자열로 표현되는 열거형이다.
    draft = "draft"
    needs_review = "needs_review"
    approved = "approved"


class EvaluationLabel(StrEnum):
    pass_ = "PASS"
    retrain = "RETRAIN"
    incomplete = "INCOMPLETE"


class Evidence(BaseModel):
    # 한 근거를 문서 → 처리 버전 → Chunk → 원문 Block/페이지까지 역추적하는 주소다.
    # 문서 버전과 OCR 재처리 버전(ingestion_id)을 분리해 과거 출처를 유지한다.
    model_config = ConfigDict(extra="forbid")

    evidence_id: str
    document_id: str
    document_version: int = Field(ge=1)
    ingestion_id: str
    chunk_id: str
    block_ids: list[str] = Field(min_length=1)
    pdf_page_number: int = Field(ge=1)


class EvidenceText(BaseModel):
    # 문장만 생성하면 출처를 잃기 쉬우므로 모든 내용에 근거 ID 목록을 붙인다.
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)


class ProcedureStep(EvidenceText):
    # 상속으로 text/evidence_ids를 재사용하고 절차에만 순서 필드를 추가한다.
    order: int = Field(ge=1)


class HandoverTask(BaseModel):
    # 한 업무를 개요·사전 조건·절차·주의사항·문제 해결로 구분한 구조다.
    model_config = ConfigDict(extra="forbid")

    task_id: str
    overview: EvidenceText
    prerequisites: list[EvidenceText]
    steps: list[ProcedureStep]
    cautions: list[EvidenceText]
    troubleshooting: list[EvidenceText]


class HandoverDocument(BaseModel):
    # None은 담당자 미확정, 빈 목록은 수집된 내용 없음 등을 표현한다.
    # missing_fields/conflicts는 확인이 필요한 항목을 사람이 검토할 때 사용한다.
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
        # after 검증은 내부 필드들이 각자의 타입 검사를 마친 뒤 실행된다.
        # 집합 차집합으로 본문에서 사용했지만 evidence 목록에 없는 ID를 찾는다.
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
    # 한 채점 기준의 획득점수와 만점이다. confidence는 점수 자체가 아닌 확신 정도다.
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
        # score와 max_score의 관계는 단일 Field 제약으로 표현할 수 없어 따로 검증한다.
        if self.score > self.max_score:
            raise ValueError("기준 점수는 만점을 초과할 수 없습니다.")
        return self


class EvaluationResult(BaseModel):
    # 점수가 없을 때 None을 사용한다. 0점과 채점 불가를 구별하기 위해서다.
    # accepted_candidate도 최종 인수인계 승인이라는 의미는 아니다.
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
