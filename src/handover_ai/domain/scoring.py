from dataclasses import dataclass

from handover_ai.domain.models import CriterionScore, EvaluationLabel, EvaluationResult

AUTO_REVIEW_THRESHOLD = 5.0
PYTHON_WEIGHT = 0.7
LLM_WEIGHT = 0.3


def normalize_criterion_scores(scores: list[CriterionScore]) -> float | None:
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
    criticality: float
    failure_impact: float
    dependency: float
    frequency: float
    urgency: float


def calculate_impact(values: ImpactInput) -> float:
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
    valid_requirements = {skill: level for skill, level in required_skills.items() if level > 0}
    total_required = sum(valid_requirements.values())
    if total_required <= 0:
        return None

    matched = sum(
        required * min(possessed_skills.get(skill, 0) / required, 1)
        for skill, required in valid_requirements.items()
    )
    return 100 * matched / total_required
