from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

from handover_ai.domain.models import CriterionScore, EvaluationResult
from handover_ai.services.evaluation import EvaluationService

router = APIRouter(prefix="/evaluations", tags=["evaluations"])


class EvaluationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    python_criteria: list[CriterionScore]
    llm_criteria: list[CriterionScore]
    evidence_valid: bool


@router.post("/compare", response_model=EvaluationResult)
def compare_evaluation(request: EvaluationRequest):
    service = EvaluationService()
    return service.evaluate(
        request.python_criteria,
        request.llm_criteria,
        request.evidence_valid,
    )
