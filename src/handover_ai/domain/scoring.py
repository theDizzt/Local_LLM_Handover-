# [읽기 안내] DB·네트워크 없이 입력으로 결과만 계산하는 순수 도메인 규칙이다.
# 테스트에서 작은 숫자 예제로 경계값을 확인할 수 있고 웹 서버 없이 재사용할 수 있다.
from dataclasses import dataclass

from handover_ai.domain.models import CriterionScore, EvaluationLabel, EvaluationResult

AUTO_REVIEW_THRESHOLD = 5.0
# 정책 값이다. 가중치는 합이 1이며 정책 변경 시 결과의 정책 버전도 함께 검토해야 한다.
PYTHON_WEIGHT = 0.7
LLM_WEIGHT = 0.3


def normalize_criterion_scores(scores: list[CriterionScore]) -> float | None:
    # 빈 목록 또는 중복 기준은 0점으로 채점하지 않고 계산 불가(None)로 돌려준다.
    # 예: 3/4점과 2/6점이면 (3+2)/(4+6)*100 = 50점이다.
    if not scores:
        return None

    criterion_ids = [item.criterion_id for item in scores]
    if len(criterion_ids) != len(set(criterion_ids)):
        return None

    total_score = sum(item.score for item in scores)
    total_max_score = sum(item.max_score for item in scores)
    if total_max_score <= 0:
        return None
    return 100 * total_score / total_max_score


def score_to_label(score: float) -> EvaluationLabel:
    # 반올림 전 실제 값으로 판정한다. 79.99를 80으로 올려 합격시키지 않는다.
    if score >= 80:
        return EvaluationLabel.pass_
    if score >= 60:
        return EvaluationLabel.retrain
    return EvaluationLabel.incomplete


def compare_scores(
    python_score: float | None,
    llm_score: float | None,
    evidence_valid: bool,
) -> EvaluationResult:
    # 한 경로라도 계산 불가이면 혼합 점수를 만들지 않고 사람 검토 대상으로 남긴다.
    if python_score is None or llm_score is None:
        return EvaluationResult(
            python_score=python_score,
            llm_score=llm_score,
            label=None,
            status="needs_review",
            needs_review=True,
            reason="두 채점 경로 중 하나 이상의 점수가 없습니다.",
        )

    difference = abs(llm_score - python_score)
    python_label = score_to_label(python_score)
    llm_label = score_to_label(llm_score)
    label_matches = python_label == llm_label
    needs_review = difference > AUTO_REVIEW_THRESHOLD or not label_matches or not evidence_valid
    # 점수 차이가 작아도 79점/81점처럼 판정이 다르면 검토가 필요하다.
    # 근거 오류는 숫자상 합의보다 우선하므로 반드시 검토 대상으로 처리한다.
    hybrid_score = PYTHON_WEIGHT * python_score + LLM_WEIGHT * llm_score

    reason = None
    if not evidence_valid:
        reason = "평가 근거가 누락되었거나 유효하지 않습니다."
    elif not label_matches:
        reason = "두 채점 경로의 판정 Label이 다릅니다."
    elif difference > AUTO_REVIEW_THRESHOLD:
        reason = "두 채점 경로의 점수 차이가 허용 범위를 초과했습니다."

    return EvaluationResult(
        python_score=python_score,
        llm_score=llm_score,
        hybrid_score=hybrid_score,
        difference=difference,
        label=score_to_label(hybrid_score),
        status="needs_review" if needs_review else "accepted_candidate",
        needs_review=needs_review,
        reason=reason,
    )


@dataclass(frozen=True, slots=True)
class ImpactInput:
    # 각 값이 같은 척도(계획상 0~100)로 제공된다는 전제다.
    # dataclass는 Pydantic과 달리 숫자 범위를 자동 검증하지 않는다.
    criticality: float
    failure_impact: float
    dependency: float
    frequency: float
    urgency: float


def calculate_impact(values: ImpactInput) -> float:
    # 중요도·실패 영향·의존성·빈도·긴급성의 가중 평균이다. 가중치 합계는 1이다.
    return (
        0.32 * values.criticality
        + 0.24 * values.failure_impact
        + 0.20 * values.dependency
        + 0.12 * values.frequency
        + 0.12 * values.urgency
    )


def calculate_skill_match(
    required_skills: dict[str, float],
    possessed_skills: dict[str, float],
) -> float | None:
    # 요구 수준이 양수인 기술만 사용하고 요구 수준을 가중치로 삼는다.
    # 보유 수준이 요구보다 높아도 min(..., 1)로 제한해 다른 기술 부족을 상쇄하지 않는다.
    # 보유 목록에 없는 기술은 0이며, 보유 수준은 음수가 아니라고 가정한다.
    valid_requirements = {skill: level for skill, level in required_skills.items() if level > 0}
    total_required = sum(valid_requirements.values())
    if total_required <= 0:
        return None

    matched = sum(
        required * min(possessed_skills.get(skill, 0) / required, 1)
        for skill, required in valid_requirements.items()
    )
    return 100 * matched / total_required
