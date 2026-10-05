# [읽기 안내] 현재 평가 API는 이미 계산된 두 경로의 점수를 비교한다.
# 이 함수에서 LLM을 호출하거나 답안 자체를 자동 채점하는 것은 아니다.
from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

from handover_ai.domain.models import CriterionScore, EvaluationResult
from handover_ai.services.evaluation import EvaluationService

router = APIRouter(prefix="/evaluations", tags=["evaluations"])


class EvaluationRequest(BaseModel):
    # 선언하지 않은 필드는 조용히 버리지 않고 오류로 처리해 입력 실수를 발견한다.
    model_config = ConfigDict(extra="forbid")

    python_criteria: list[CriterionScore]
    llm_criteria: list[CriterionScore]
    evidence_valid: bool
    # 현재는 호출자가 전달한 근거 검증 결과다. DB 근거 조회를 대신 수행하지 않는다.


@router.post("/compare", response_model=EvaluationResult)
def compare_evaluation(request: EvaluationRequest):
    service = EvaluationService()
    return service.evaluate(
        request.python_criteria,
        request.llm_criteria,
        request.evidence_valid,
    )
