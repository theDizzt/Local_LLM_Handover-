from handover_ai.domain.models import CriterionScore, EvaluationResult
from handover_ai.domain.scoring import compare_scores, normalize_criterion_scores


class EvaluationService:
    def evaluate(
        self,
        python_criteria: list[CriterionScore],
        llm_criteria: list[CriterionScore],
        evidence_valid: bool,
    ) -> EvaluationResult:
        python_score = normalize_criterion_scores(python_criteria)
        llm_score = normalize_criterion_scores(llm_criteria)
        return compare_scores(python_score, llm_score, evidence_valid)
