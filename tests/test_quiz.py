"""문항 형식·원문 대조 검증·채점·QuizScore를 확인한다 (quiz-design.md 구현 순서 1단계)."""

import pytest

from handover_ai.domain import quiz
from handover_ai.domain import quiz_guard as guard

SOURCES = {
    "B1": {
        "block_id": "blk-1",
        "text": "기록관리기준표의 기능분류는 정책분야, 정책영역, 대기능, 중기능, 소기능, "
        "단위과제의 6단계로 구성된다.",
    },
    "B2": {"block_id": "blk-2", "text": "보존기간은 공공기록물법 시행령 제26조에 따라 정한다."},
}


def short(stem="기록관리기준표의 기능분류는 몇 단계로 구성되는가?", quote="6 단계", **extra):
    return {
        "kind": "short_answer",
        "stem": stem,
        "answer": {"block": "B1", "quote": quote},
        "evidence_blocks": ["B1"],
        **extra,
    }


def choice(distractors=("4단계", "5단계", "7단계"), **extra):
    return {
        "kind": "multiple_choice",
        "stem": "기록관리기준표 기능분류의 단계 수는?",
        "answer": {"block": "B1", "quote": "6단계"},
        "distractors": list(distractors),
        "evidence_blocks": ["B1"],
        **extra,
    }


def test_find_span_returns_source_text_despite_spacing():
    text = SOURCES["B1"]["text"]
    assert quiz.find_span(text, "6 단계") == "6단계"
    assert quiz.find_span(text, "대기능,중기능") == "대기능, 중기능"
    assert quiz.find_span(text, "7단계") is None
    assert quiz.find_span(text, " ,") is None


def test_short_answer_uses_source_span_not_model_text():
    result = guard.check("TASK-001", short(), SOURCES)
    assert result.failures == ()
    assert result.question.answer == "6단계"
    assert result.question.evidence_block_ids == ["blk-1"]
    assert result.question.status == "draft"


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        (short(quote="8단계"), "quote_not_in_source"),
        (short(stem="공공기록물법 시행규칙상 기능분류는 몇 단계인가?"), "ungrounded_token"),
        (short(stem="2007년 개정된 기준표의 기능분류는 몇 단계인가?"), "ungrounded_token"),
        (short(stem="BRM 기준표의 기능분류는 몇 단계인가?"), "ungrounded_token"),
        (short(stem="기능분류 6단계 중 마지막 단계는 무엇인가?"), "answer_leak"),
        (short(quote=SOURCES["B1"]["text"]), "answer_too_long"),
        ({**short(), "answer": {"block": "B9", "quote": "6단계"}}, "unknown_block"),
        ({**short(), "distractors": ["a", "b", "c"]}, "schema_invalid"),
        ({**short(), "reason": "추가 필드"}, "schema_invalid"),
        (choice(distractors=("5단계", "5 단계", "7단계")), "duplicate_option"),
        (choice(distractors=("단위과제", "5단계", "7단계")), "distractor_in_source"),
        (choice(distractors=("5단계", "7단계")), "schema_invalid"),
    ],
)
def test_guard_rejects_with_reason(raw, code):
    result = guard.check("TASK-001", raw, SOURCES)
    assert result.question is None
    assert code in result.failures


def test_grounded_tokens_pass_including_law_article():
    raw = {
        "kind": "short_answer",
        "stem": "공공기록물법 시행령 제26조는 무엇을 정하는 근거인가?",
        "answer": {"block": "B2", "quote": "보존기간"},
        "evidence_blocks": ["B2"],
    }
    assert guard.check("TASK-002", raw, SOURCES).failures == ()
    # '방법'처럼 법령 접미사로 끝나는 일반 낱말은 대조 대상이 아니다.
    assert guard.checked_tokens("보존기간을 정하는 방법은?") == []


def test_multiple_choice_requires_distractor_review_and_flags_outside_numbers():
    result = guard.check("TASK-001", choice(), SOURCES)
    question = result.question
    assert result.failures == ()
    assert question.options[question.answer_order[0]] == "6단계"
    assert question.review_required_fields == ["distractors"]
    # 오답 보기의 숫자는 원래 원문에 없으므로 탈락이 아니라 검토 표시다.
    assert "distractor_ungrounded_token" in result.flags
    # 같은 문항은 언제 만들어도 같은 보기 순서다.
    assert guard.check("TASK-001", choice(), SOURCES).question == question


def test_duplicates_within_response_and_existing():
    results = guard.check_all(
        "TASK-001",
        [short(), short(stem="기능분류의 단계 수는 몇 개인가?"), choice()],
        SOURCES,
    )
    assert [r.failures for r in results] == [(), ("duplicate_question",), ()]
    again = guard.check_all("TASK-001", [short()], SOURCES, [results[0].question])
    assert again[0].failures == ("duplicate_question",)


def test_ordering_question_is_built_without_model():
    steps = [quiz.Step(f"{n}단계 작업을 한다.", (f"b{n}",)) for n in range(1, 5)]
    question = quiz.ordering_question("TASK-003", "보존처리", steps)
    assert question.options != [s.text for s in steps]
    assert [question.options[i] for i in question.answer_order] == [s.text for s in steps]
    assert question.evidence_block_ids == ["b1", "b2", "b3", "b4"]
    assert quiz.ordering_question("TASK-003", "보존처리", steps) == question
    assert quiz.ordering_question("TASK-003", "보존처리", steps[:2]) is None
    assert quiz.ordering_question("TASK-003", "보존처리", [steps[0]] * 3) is None


def test_scoring_rules():
    short_q = guard.check("TASK-001", short(), SOURCES).question
    assert quiz.score_answer(short_q, " 6 단계 ").score == 1
    assert quiz.score_answer(short_q, "여섯 단계").score == 0
    assert (
        quiz.score_answer(
            short_q.model_copy(update={"accepted_answers": ["여섯 단계"]}), "여섯단계"
        ).score
        == 1
    )
    assert quiz.score_answer(short_q, "").score == 0

    choice_q = guard.check("TASK-001", choice(), SOURCES).question
    assert quiz.score_answer(choice_q, choice_q.answer_order[0]).score == 1
    with pytest.raises(quiz.InvalidAnswer):
        quiz.score_answer(choice_q, 4)
    with pytest.raises(quiz.InvalidAnswer):
        quiz.score_answer(choice_q, True)

    steps = [quiz.Step(f"{n}단계 작업을 한다.", (f"b{n}",)) for n in range(1, 5)]
    order_q = quiz.ordering_question("TASK-003", "보존처리", steps)
    a, b, c, d = order_q.answer_order
    assert quiz.score_answer(order_q, [a, b, c, d]) == quiz.Score(1, 1, "rule_order")
    assert quiz.score_answer(order_q, [b, c, d, a]).score == pytest.approx(2 / 3)
    assert quiz.score_answer(order_q, [d, c, b, a]).score == 0
    with pytest.raises(quiz.InvalidAnswer):
        quiz.score_answer(order_q, [a, a, b, c])


def test_question_rejects_inconsistent_answer():
    with pytest.raises(ValueError):
        quiz.Question(
            question_id="Q", task_id="T", kind="multiple_choice", stem="?",
            options=["a", "b"], answer_order=[0], evidence_block_ids=["b"],
        )  # fmt: skip


def result(task, score, full=1):
    return quiz.AnswerResult(task, f"{task}-{score}", score, full)


def test_quiz_score_is_impact_weighted():
    impacts = {"T1": 90, "T2": 30, "T3": 60}
    value = quiz.quiz_score(
        impacts, {"T1", "T2"}, [result("T1", 1), result("T1", 0), result("T2", 1)]
    )
    # T1 50점·T2 100점 → (90×50 + 30×100) / 120 = 62.5. 단순 평균이면 66.7이다.
    assert value.value == pytest.approx(62.5)
    assert value.task_scores == {"T1": 50, "T2": 100}
    assert value.reasons == ()


def test_quiz_score_requires_quizzed_critical_tasks():
    impacts = {"T1": 90, "T2": 30, "T3": 85}
    value = quiz.quiz_score(impacts, {"T1", "T2"}, [result("T2", 1)])
    assert value.value is None
    assert value.missing_critical_task_ids == ("T1",)
    assert value.reasons == ("critical_task_quiz_missing",)
    # 문항이 아직 없는 핵심 업무(T3)는 풀 수 없으므로 막지 않는다.
    assert quiz.quiz_score(impacts, {"T1", "T2"}, [result("T1", 1)]).value == 100
    assert quiz.quiz_score(impacts, set(), []).reasons == ("quiz_score_missing",)
    assert quiz.quiz_score({"T1": 0}, {"T1"}, [result("T1", 1)]).reasons == ("total_impact_zero",)
