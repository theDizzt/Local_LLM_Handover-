# [읽기 안내] 점수 환산·판정 경계·업무 영향도·기술 적합도를 숫자 예제로 확인한다.
# 79.99 같은 경계값과 중복 기준을 포함해 '그럴듯한 잘못된 점수'가 나오지 않도록 한다.
import unittest

from handover_ai.domain.models import CriterionScore, EvaluationLabel
from handover_ai.domain.scoring import (
    ImpactInput,
    calculate_impact,
    calculate_skill_match,
    compare_scores,
    normalize_criterion_scores,
    score_to_label,
)


def criterion(criterion_id: str, score: float, max_score: float):
    return CriterionScore(
        criterion_id=criterion_id,
        criterion=criterion_id,
        score=score,
        max_score=max_score,
        source_evidence_ids=["EV-001"],
        confidence=1,
    )


class ScoringTest(unittest.TestCase):
    def test_normalizes_criterion_scores(self):
        result = normalize_criterion_scores(
            [
                criterion("R1", 3, 3),
                criterion("R2", 1, 2),
            ]
        )
        self.assertEqual(result, 80)

    def test_rejects_duplicate_criteria(self):
        result = normalize_criterion_scores(
            [
                criterion("R1", 1, 2),
                criterion("R1", 1, 2),
            ]
        )
        self.assertIsNone(result)

    def test_label_boundaries_use_unrounded_score(self):
        self.assertEqual(score_to_label(80), EvaluationLabel.pass_)
        self.assertEqual(score_to_label(79.999), EvaluationLabel.retrain)
        self.assertEqual(score_to_label(60), EvaluationLabel.retrain)
        self.assertEqual(score_to_label(59.999), EvaluationLabel.incomplete)

    def test_small_difference_with_same_label_is_candidate(self):
        result = compare_scores(78, 75, True)
        self.assertFalse(result.needs_review)
        self.assertEqual(result.status, "accepted_candidate")

    def test_small_difference_with_different_label_needs_review(self):
        result = compare_scores(81, 79, True)
        self.assertTrue(result.needs_review)
        self.assertIn("Label", result.reason)

    def test_calculates_impact(self):
        result = calculate_impact(ImpactInput(100, 100, 100, 100, 100))
        self.assertEqual(result, 100)

    def test_calculates_weighted_skill_match(self):
        result = calculate_skill_match(
            {"CAN": 4, "STM32": 2},
            {"CAN": 2, "STM32": 2},
        )
        self.assertAlmostEqual(result, 66.6666666667)


if __name__ == "__main__":
    unittest.main()
