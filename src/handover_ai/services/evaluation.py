# [읽기 안내] 서비스는 한 번의 사용자 작업에 필요한 도메인 함수를 순서대로 조합한다.
# 이 서비스는 '기준별 점수 목록 → 100점 환산 → 두 점수 비교'만 수행한다.
from handover_ai.domain.models import CriterionScore, EvaluationResult
from handover_ai.domain.scoring import compare_scores, normalize_criterion_scores


class EvaluationService:
    def evaluate(
        self,
        python_criteria: list[CriterionScore],
        llm_criteria: list[CriterionScore],
        evidence_valid: bool,
    ) -> EvaluationResult:
        # 항목별 만점이 달라도 합산한 획득점수/만점 비율로 같은 100점 척도를 만든다.
        python_score = normalize_criterion_scores(python_criteria)
        llm_score = normalize_criterion_scores(llm_criteria)
        return compare_scores(python_score, llm_score, evidence_valid)
