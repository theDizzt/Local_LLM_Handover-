"""[읽기 안내] 업무 이해도 문항의 형식·채점·QuizScore를 정하는 순수 규칙이다 (quiz-design.md).

모델은 문제 문장과 "어느 Block의 어느 구간이 정답인지"만 제안한다. 정답 문자열은 저장된 원문에서
잘라 만들고(`find_span`), 원문과의 대조는 quiz_guard.py가 맡는다. 절차 순서 문항은 원문 순서가
곧 정답이므로 모델 없이 여기서 만든다.

채점은 모두 규칙 비교(Python Score)다. 서술형과 LLM 채점은 후속 단계에서 추가한다.
계산할 수 없는 값은 0으로 채우지 않고 None과 사유 코드를 돌려준다.
"""

import hashlib
import unicodedata
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

VERSION = "quiz-v1"
CRITICAL_IMPACT = 80
MIN_ORDER_STEPS = 3
MAX_ORDER_STEPS = 6
MAX_SHORT_ANSWER = 40
DISTRACTORS = 3
Kind = Literal["ordering", "short_answer", "multiple_choice"]
Status = Literal["draft", "approved", "rejected", "retired"]


def _normalized_chars(text):
    # 글자마다 (정규화 글자, 원문 위치)를 만든다. 전각/반각·호환 문자를 통일(NFKC)하고
    # 대소문자·공백·문장부호 차이를 없앤다. OCR 띄어쓰기 오류로 정답이 틀리지 않게 하기 위해서다.
    for index, char in enumerate(text):
        for normal in unicodedata.normalize("NFKC", char).casefold():
            if normal.isalnum():
                yield normal, index


def normalize(text: str) -> str:
    return "".join(char for char, _ in _normalized_chars(text))


def find_span(text: str, quote: str) -> str | None:
    """quote와 정규화 결과가 같은 원문 구간을 원문 그대로 돌려준다. 없으면 None.

    모델이 띄어쓰기나 문장부호를 바꿔 인용해도 정답 문자열은 원문에서 가져온다.
    """
    target = normalize(quote)
    if not target:
        return None
    pairs = list(_normalized_chars(text))
    start = "".join(char for char, _ in pairs).find(target)
    if start < 0:
        return None
    return text[pairs[start][1] : pairs[start + len(target) - 1][1] + 1]


class Question(BaseModel):
    """저장·출제 단위. options는 화면에 보이는 순서이고 정답은 위치(index)로 가리킨다."""

    model_config = ConfigDict(extra="forbid")

    question_id: str
    task_id: str
    kind: Kind
    stem: str = Field(min_length=1)
    options: list[str] = Field(default_factory=list)
    # short_answer: 원문 정답 구간. 그 외 유형은 None.
    answer: str | None = None
    # multiple_choice: [정답 위치]. ordering: 원문 순서대로 나열한 options 위치.
    answer_order: list[int] = Field(default_factory=list)
    # 검토자가 승인한 동의 표현. 단답에서만 쓴다.
    accepted_answers: list[str] = Field(default_factory=list)
    evidence_block_ids: list[str] = Field(min_length=1)
    max_score: float = Field(default=1, gt=0)
    status: Status = "draft"
    # 객관식 오답 보기처럼 원문에 없는 내용은 검토자가 확인해야 승인할 수 있다.
    review_required_fields: list[str] = Field(default_factory=list)
    quiz_version: str = VERSION

    @model_validator(mode="after")
    def validate_answer(self):
        if self.kind == "short_answer":
            if not self.answer or self.options or self.answer_order:
                raise ValueError("단답은 answer만 가진다.")
        elif self.kind == "multiple_choice":
            if len(self.options) != DISTRACTORS + 1 or len(self.answer_order) != 1:
                raise ValueError("객관식은 보기 4개와 정답 위치 1개를 가진다.")
        elif sorted(self.answer_order) != list(range(len(self.options))) or not (
            MIN_ORDER_STEPS <= len(self.options) <= MAX_ORDER_STEPS
        ):
            raise ValueError("순서 문항의 answer_order는 모든 보기의 순열이어야 한다.")
        if any(not 0 <= i < len(self.options) for i in self.answer_order):
            raise ValueError("정답 위치가 보기 범위를 벗어났다.")
        return self


def question_id(task_id, kind, stem, answer_text):
    # 같은 업무·유형·문제·정답이면 같은 ID다. 재생성해도 중복 저장되지 않는다.
    key = "\x1f".join([VERSION, task_id, kind, normalize(stem), normalize(answer_text)])
    return "Q-" + hashlib.sha256(key.encode()).hexdigest()[:16]


def shuffled(items, seed, *, avoid_identity):
    """seed로 정해지는 순서. 같은 문항은 언제 만들어도 같은 보기 순서가 나온다.

    순서 문항은 섞은 결과가 원래 순서와 같으면 문제가 되지 않으므로 avoid_identity로 피한다.
    """
    keyed = sorted(
        range(len(items)), key=lambda i: hashlib.sha256(f"{seed}:{i}".encode()).hexdigest()
    )
    if avoid_identity and keyed == sorted(keyed):
        keyed = keyed[1:] + keyed[:1]
    return keyed


@dataclass(frozen=True, slots=True)
class Step:
    """절차 한 단계. 원문 읽기 순서의 연속 Block을 이어 붙인 문장이다."""

    text: str
    block_ids: tuple[str, ...]


def ordering_question(task_id: str, title: str, steps: list[Step]) -> Question | None:
    # 모델 없이 원문 순서로 만든다. 단계가 너무 적거나 많거나, 같은 문장이 반복되면
    # 순서를 하나로 정할 수 없으므로 만들지 않는다.
    texts = [step.text.strip() for step in steps]
    if not MIN_ORDER_STEPS <= len(texts) <= MAX_ORDER_STEPS or not all(texts):
        return None
    if len({normalize(text) for text in texts}) != len(texts):
        return None
    stem = f"'{title}' 업무의 절차를 원문 순서대로 나열하시오."
    qid = question_id(task_id, "ordering", stem, "\x1e".join(texts))
    order = shuffled(texts, qid, avoid_identity=True)
    return Question(
        question_id=qid,
        task_id=task_id,
        kind="ordering",
        stem=stem,
        options=[texts[i] for i in order],
        # 원문 i번째 단계가 보기의 어느 위치에 있는지
        answer_order=[order.index(i) for i in range(len(texts))],
        evidence_block_ids=list(dict.fromkeys(b for step in steps for b in step.block_ids)),
    )


# ── 채점 ──────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Score:
    score: float
    max_score: float
    mode: str


class InvalidAnswer(ValueError):
    """형식이 맞지 않는 답안. 0점이 아니라 제출 거부로 처리한다."""


# ── 검토 ──────────────────────────────────────────────────────────────

ReviewAction = Literal["approve", "reject", "retire"]
# 확정 문항은 고치지 않는다. 내용을 바꾸려면 새 문항을 만들고 이전 문항을 retired로 둔다.
# 이미 제출된 답안이 어떤 문항 기준으로 채점됐는지 보존하기 위해서다.
TRANSITIONS = {
    ("draft", "approve"): "approved",
    ("draft", "reject"): "rejected",
    ("approved", "retire"): "retired",
}


class QuestionConflict(ValueError):
    """다른 검토가 먼저 저장되었거나 현재 상태에서 할 수 없는 검토."""


class InvalidReview(ValueError):
    """검토 필수 항목을 확인하지 않은 승인 등 요청 자체가 규칙에 맞지 않음."""


def review_status(question: Question, action: ReviewAction, confirmed_fields: list[str]) -> Status:
    target = TRANSITIONS.get((question.status, action))
    if target is None:
        raise QuestionConflict(f"{question.status} 상태의 문항에는 {action}을(를) 할 수 없습니다.")
    missing = set(question.review_required_fields) - set(confirmed_fields)
    if action == "approve" and missing:
        # 객관식 오답 보기는 원문에 없는 문장이라 사람이 확인해야만 승인한다.
        raise InvalidReview(f"검토 필수 항목을 확인해 주세요: {', '.join(sorted(missing))}")
    return target


def score_answer(question: Question, answer) -> Score:
    if question.kind == "short_answer":
        if not isinstance(answer, str):
            raise InvalidAnswer("단답은 문자열로 제출한다.")
        accepted = {normalize(text) for text in [question.answer, *question.accepted_answers]}
        hit = bool(normalize(answer)) and normalize(answer) in accepted
        return Score(question.max_score if hit else 0, question.max_score, "rule_exact")
    if question.kind == "multiple_choice":
        if not isinstance(answer, int) or isinstance(answer, bool):
            raise InvalidAnswer("객관식은 보기 위치(정수)로 제출한다.")
        if not 0 <= answer < len(question.options):
            raise InvalidAnswer("보기 범위를 벗어났다.")
        hit = answer == question.answer_order[0]
        return Score(question.max_score if hit else 0, question.max_score, "rule_exact")
    if not isinstance(answer, list) or sorted(answer) != list(range(len(question.options))):
        raise InvalidAnswer("순서 문항은 모든 보기 위치를 한 번씩 나열한다.")
    # 인접 쌍 일치율: 한 단계만 밀려도 0점이 되는 완전 일치보다 이해 정도를 잘 드러낸다.
    # 예: 정답 A→B→C→D, 답안 B→C→D→A이면 (B,C),(C,D)가 맞아 2/3.
    expected = set(zip(question.answer_order, question.answer_order[1:], strict=False))
    given = set(zip(answer, answer[1:], strict=False))
    ratio = len(expected & given) / len(expected)
    return Score(question.max_score * ratio, question.max_score, "rule_order")


# ── QuizScore ─────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class AnswerResult:
    """문항별 최신 답안의 채점 결과. 같은 문항을 여러 번 풀었으면 저장소가 최신 것만 넘긴다."""

    task_id: str
    question_id: str
    score: float
    max_score: float


@dataclass(frozen=True, slots=True)
class QuizScore:
    value: float | None
    task_scores: dict[str, float]
    answered_task_ids: tuple[str, ...]
    quizzed_task_ids: tuple[str, ...]
    missing_critical_task_ids: tuple[str, ...]
    reasons: tuple[str, ...] = field(default=())


def quiz_score(
    impacts: dict[str, float], quizzed_task_ids: set[str], results: list[AnswerResult]
) -> QuizScore:
    """업무 영향도로 가중한 문항 점수 (2026-10-19 결정).

    TaskQuizScore = 100 × Σ획득 / Σ만점 (업무별), QuizScore = Σ Impact×TaskQuizScore / Σ Impact.
    approved 문항이 있는 핵심 업무(Impact 80 이상)를 하나라도 풀지 않았으면 None이다.
    쉬운 업무만 풀어 준비도가 높아지는 것을 막기 위해서다. 문항이 아직 없는 업무는
    풀 수 없으므로 막지 않고 quizzed_task_ids로 범위를 드러낸다.
    """
    totals: dict[str, list[float]] = {}
    for result in results:
        if result.task_id not in impacts:
            continue
        got, full = totals.setdefault(result.task_id, [0.0, 0.0])
        totals[result.task_id] = [got + result.score, full + result.max_score]
    task_scores = {task: 100 * got / full for task, (got, full) in totals.items() if full > 0}
    missing = tuple(
        sorted(
            task
            for task in quizzed_task_ids
            if impacts.get(task, 0) >= CRITICAL_IMPACT and task not in task_scores
        )
    )
    reasons = []
    if not task_scores:
        reasons.append("quiz_score_missing")
    if missing:
        reasons.append("critical_task_quiz_missing")
    weight = sum(impacts[task] for task in task_scores)
    if task_scores and weight <= 0:
        reasons.append("total_impact_zero")
    value = None
    if not reasons:
        value = sum(impacts[task] * score for task, score in task_scores.items()) / weight
    return QuizScore(
        value,
        task_scores,
        tuple(sorted(task_scores)),
        tuple(sorted(quizzed_task_ids & impacts.keys())),
        missing,
        tuple(reasons),
    )
