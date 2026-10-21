"""[읽기 안내] 모델이 제안한 문항을 원문과 대조해 통과·탈락을 정하는 순수 규칙이다.

로컬 모델도 사전학습 지식이 있으므로 원문에 없는 숫자·기간·법령명을 문제에 섞을 수 있다.
여기서는 모델 출력을 "원문에서 확인 가능한 것"으로 제한한다.

- 정답: 모델 인용이 아니라 원문에서 잘라 낸 구간(`quiz.find_span`)을 쓴다.
- 문제·보기: 숫자·조항·법령명·영문 약어를 뽑아 근거 원문에 있는지 본다.
- 객관식 오답 보기는 원래 원문에 없는 문장이다. 탈락시키지 않고 검토 필수로 표시한다.

외부 지식을 모두 막는 장치는 아니다. 기계적으로 대조할 수 있는 흔한 형태를 걸러 내고,
나머지는 사람 검토(`status=draft` → `approved`)에 맡긴다.
"""

import re
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from handover_ai.domain import quiz

VERSION = "quiz-guard-v1"
MAX_PROPOSALS = 10

# 단위가 붙은 숫자(6단계, 10년, 3.5%)와 단독 숫자. 원문 쪽은 정규화 후 포함 여부로 비교한다.
_NUMBER = re.compile(r"\d+(?:[.,]\d+)*\s*(?:년|개월|월|일|시간|분|회|단계|개|명|%|쪽|조|항|호|종)?")
_ARTICLE = re.compile(r"제\s*\d+\s*(?:조|항|호)(?:\s*의\s*\d+)?")
_ACRONYM = re.compile(r"[A-Za-z][A-Za-z0-9\-]*[A-Z][A-Za-z0-9\-]*|[A-Z]{2,}")
_HANGUL_WORD = re.compile(r"[가-힣]+")
_LAW_SUFFIX = re.compile(r"(?:법률|시행령|시행규칙|법|령|규칙|규정|지침|고시|훈령|예규)$")
# 법령 접미사로 끝나지만 고유한 법령 이름이 아닌 일반 낱말
_LAW_STOPWORDS = {
    "방법", "기법", "해법", "용법", "수법", "문법", "편법", "불법", "위법", "적법", "합법",
    "명령", "지령", "수령", "요령", "연령", "대통령", "법령", "규정", "규칙", "지침", "고시",
    "시행령", "시행규칙", "법률",
}  # fmt: skip
_PARTICLES = (
    "에서",
    "으로",
    "에는",
    "에",
    "의",
    "을",
    "를",
    "은",
    "는",
    "이",
    "가",
    "로",
    "과",
    "와",
    "상",
)


class AnswerSpan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    block: str = Field(min_length=1, max_length=20)
    quote: str = Field(min_length=1, max_length=200)


class ProposedQuestion(BaseModel):
    """모델 응답 한 문항. block 값은 프롬프트에 준 별칭(B1…)이다."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["short_answer", "multiple_choice"]
    stem: str = Field(min_length=5, max_length=300)
    answer: AnswerSpan
    distractors: list[str] = Field(default_factory=list, max_length=quiz.DISTRACTORS)
    evidence_blocks: list[str] = Field(min_length=1, max_length=5)

    @model_validator(mode="after")
    def validate_distractors(self):
        expected = quiz.DISTRACTORS if self.kind == "multiple_choice" else 0
        if len(self.distractors) != expected:
            raise ValueError(f"{self.kind}의 오답 보기는 {expected}개여야 한다.")
        return self


@dataclass(frozen=True, slots=True)
class GuardResult:
    question: quiz.Question | None
    failures: tuple[str, ...]
    # 통과 여부와 무관하게 검토자에게 보여 줄 표시 (예: 오답 보기의 원문 밖 숫자)
    flags: tuple[str, ...] = field(default=())
    proposal: dict | None = None


def _law_names(text):
    for word in _HANGUL_WORD.findall(text):
        for particle in _PARTICLES:
            if word.endswith(particle) and len(word) - len(particle) >= 2:
                word = word[: -len(particle)]
                break
        if len(word) >= 3 and _LAW_SUFFIX.search(word) and word not in _LAW_STOPWORDS:
            yield word


def _answer_text(question):
    if question.kind == "short_answer":
        return quiz.normalize(question.answer)
    if question.kind == "multiple_choice":
        return quiz.normalize(question.options[question.answer_order[0]])
    return None


def checked_tokens(text: str) -> list[str]:
    """원문 대조 대상 토큰. 외부 지식이 섞일 때 흔한 숫자·조항·법령명·영문 약어다."""
    tokens = [m.group().strip() for m in _ARTICLE.finditer(text)]
    tokens += [m.group().strip() for m in _NUMBER.finditer(text)]
    tokens += _ACRONYM.findall(text)
    tokens += list(_law_names(text))
    return list(dict.fromkeys(t for t in tokens if quiz.normalize(t)))


def ungrounded(text: str, source: str) -> list[str]:
    normal = quiz.normalize(source)
    return [token for token in checked_tokens(text) if quiz.normalize(token) not in normal]


def check(
    task_id: str,
    raw: dict,
    sources: dict[str, dict],
    existing: list[quiz.Question] = (),
) -> GuardResult:
    """모델 응답 한 문항(raw)을 검사한다.

    sources는 {별칭: {"block_id", "text"}}다. existing은 같은 업무에 이미 있는 문항과
    같은 응답에서 먼저 통과한 문항이며 중복 판정에 쓴다.
    """
    try:
        proposal = ProposedQuestion.model_validate(raw)
    except ValidationError:
        return GuardResult(
            None, ("schema_invalid",), proposal=raw if isinstance(raw, dict) else None
        )

    failures, flags = [], []
    aliases = list(dict.fromkeys([proposal.answer.block, *proposal.evidence_blocks]))
    if any(alias not in sources for alias in aliases):
        return GuardResult(None, ("unknown_block",), proposal=raw)
    evidence = "\n".join(sources[alias]["text"] for alias in aliases)

    # 정답은 모델이 쓴 문자열이 아니라 원문에서 잘라 낸 구간이다.
    answer = quiz.find_span(sources[proposal.answer.block]["text"], proposal.answer.quote)
    if answer is None:
        failures.append("quote_not_in_source")
        answer = proposal.answer.quote
    normal_answer = quiz.normalize(answer)
    if proposal.kind == "short_answer" and len(normal_answer) > quiz.MAX_SHORT_ANSWER:
        failures.append("answer_too_long")
    if normal_answer and normal_answer in quiz.normalize(proposal.stem):
        failures.append("answer_leak")
    if ungrounded(proposal.stem, evidence):
        failures.append("ungrounded_token")

    options = []
    if proposal.kind == "multiple_choice":
        options = [answer, *(d.strip() for d in proposal.distractors)]
        normals = [quiz.normalize(option) for option in options]
        if not all(normals) or len(set(normals)) != len(normals):
            failures.append("duplicate_option")
        # 오답 보기가 근거 원문에 들어 있으면 정답이 둘일 수 있다.
        source_normal = quiz.normalize(evidence)
        if any(normal in source_normal for normal in normals[1:] if normal):
            failures.append("distractor_in_source")
        for distractor in proposal.distractors:
            if ungrounded(distractor, evidence):
                flags.append("distractor_ungrounded_token")
                break

    qid = quiz.question_id(task_id, proposal.kind, proposal.stem, answer)
    stem_normal = quiz.normalize(proposal.stem)
    for other in existing:
        same_answer = other.kind == proposal.kind and _answer_text(other) == normal_answer
        if other.question_id == qid or quiz.normalize(other.stem) == stem_normal or same_answer:
            failures.append("duplicate_question")
            break

    if failures:
        return GuardResult(None, tuple(dict.fromkeys(failures)), tuple(flags), raw)

    block_ids = list(dict.fromkeys(sources[alias]["block_id"] for alias in aliases))
    if proposal.kind == "short_answer":
        question = quiz.Question(
            question_id=qid,
            task_id=task_id,
            kind="short_answer",
            stem=proposal.stem.strip(),
            answer=answer,
            evidence_block_ids=block_ids,
        )
    else:
        # 정답 위치가 늘 첫 번째가 되지 않도록 문항 ID로 정해지는 순서로 섞는다.
        order = quiz.shuffled(options, qid, avoid_identity=False)
        question = quiz.Question(
            question_id=qid,
            task_id=task_id,
            kind="multiple_choice",
            stem=proposal.stem.strip(),
            options=[options[i] for i in order],
            answer_order=[order.index(0)],
            evidence_block_ids=block_ids,
            review_required_fields=["distractors"],
        )
    return GuardResult(question, (), tuple(flags), raw)


def check_all(task_id, raws, sources, existing=()):
    """응답 전체를 검사한다. 앞에서 통과한 문항도 뒤 문항의 중복 판정에 포함한다."""
    accepted, results = list(existing), []
    for raw in list(raws)[:MAX_PROPOSALS]:
        result = check(task_id, raw, sources, accepted)
        results.append(result)
        if result.question:
            accepted.append(result.question)
    return results
