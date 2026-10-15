"""학습한 어댑터를 승격 후보로 등록할지 판정한다.

기준 모델(어댑터 없음)과 같은 실제 문서 문항·회귀 사례로 비교한다. 하나라도 나빠지면
탈락이다. 통과해도 자동 적용하지 않고 사람이 보고서를 확인한 뒤 활성화한다.
"""

LATENCY_TOLERANCE = 1.2


def p95(values):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(0.95 * (len(ordered) - 1)))]


def judge(baseline, candidate, regression_passed, baseline_seconds, candidate_seconds):
    """두 summary.json과 회귀 결과로 게이트 항목별 통과 여부를 계산한다."""
    base, cand = baseline["outcomes"], candidate["outcomes"]

    def count(outcomes, key):
        return outcomes.get(key, 0)

    def selected(summary):
        return summary["quote_coverage"]["selected_quote_exact"]["matched"]

    base_p95, cand_p95 = p95(baseline_seconds), p95(candidate_seconds)
    checks = [
        {
            "name": "regression_suite",
            "rule": "기존 Qwen 회귀 사례 전체 통과",
            "passed": bool(regression_passed),
        },
        {
            "name": "complete",
            "rule": "실제 문서 문항 전체 실행",
            "baseline": baseline["completed_questions"],
            "candidate": candidate["completed_questions"],
            "passed": candidate["completed_questions"] == baseline["completed_questions"],
        },
        {
            "name": "false_answer",
            "rule": "무답 질문 오선택 증가 금지",
            "baseline": count(base, "false_answer"),
            "candidate": count(cand, "false_answer"),
            "passed": count(cand, "false_answer") <= count(base, "false_answer"),
        },
        {
            "name": "execution_error",
            "rule": "시간 초과·모델 오류 증가 금지",
            "baseline": count(base, "execution_error"),
            "candidate": count(cand, "execution_error"),
            "passed": count(cand, "execution_error") <= count(base, "execution_error"),
        },
        {
            "name": "missed_answer",
            "rule": "유답 질문 근거 없음 처리 증가 금지",
            "baseline": count(base, "missed_answer"),
            "candidate": count(cand, "missed_answer"),
            "passed": count(cand, "missed_answer") <= count(base, "missed_answer"),
        },
        {
            "name": "selected_quote_exact",
            "rule": "최종 선택 정답 인용문 감소 금지",
            "baseline": selected(baseline),
            "candidate": selected(candidate),
            "passed": selected(candidate) >= selected(baseline),
        },
        {
            "name": "improvement",
            "rule": "근거 없음 처리 감소 또는 정답 인용문 증가 중 하나 이상",
            "passed": count(cand, "missed_answer") < count(base, "missed_answer")
            or selected(candidate) > selected(baseline),
        },
        {
            "name": "latency_p95",
            "rule": f"문항 처리 시간 p95가 기준의 {LATENCY_TOLERANCE}배 이하",
            "baseline": base_p95,
            "candidate": cand_p95,
            "passed": base_p95 is not None
            and cand_p95 is not None
            and cand_p95 <= base_p95 * LATENCY_TOLERANCE,
        },
    ]
    return {"passed": all(check["passed"] for check in checks), "checks": checks}
