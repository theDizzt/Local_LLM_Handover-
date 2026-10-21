# 문제 생성·QuizScore 설계안 (quiz-v1)

2026-10-19 작성, 구현 전 설계. [로드맵](../roadmap.md) 2순위 작업이다. 구현 후 실제 동작은
별도 주제 문서(`docs/quiz.md`)로 옮긴다.

## 목표와 범위

스캔 PDF → 문제 → 점수 → 준비도·추천 흐름의 빈칸을 채운다. 완료되면 `readiness`가
`quiz_score_missing` 없이 값을 돌려준다.

| 포함 (v1) | 제외 (후속) |
|---|---|
| 업무별 문제 생성: 절차 순서·단답·객관식 | 서술형 문제와 LLM 채점 (로드맵 3순위) |
| 원문 대조 검증(Quiz Guard)과 사람 확정 | 문제 편집 화면 (API로 먼저 제공) |
| 답안 저장, Python 채점, QuizScore → Readiness | 난이도 자동 보정, LLM D 설명 |

## 핵심 원칙: 모델은 제안하고, Python이 원문과 대조한다

로컬 모델도 사전학습 지식을 갖고 있으므로 네트워크 차단만으로 외부 지식을 막을 수 없다.
초안 생성의 "모델은 ID만 고르고 본문은 SQLite에서 조립" 원칙을 문제 생성에도 최대한 적용한다.

1. **정답은 원문에서 그대로 가져온다.** 모델은 정답을 쓰지 않고 `블록 별칭 + 인용 구간`을 지정한다.
   정답 문자열은 저장된 Block 본문에서 잘라 만든다.
2. **모든 문항에 근거 Block ID가 필수다.** 제공하지 않은 ID, 원문에 없는 인용은 탈락이다.
3. **숫자·기간·법령·고유명사 대조.** 문제 문장과 보기에서 추출한 토큰이 근거 원문에 없으면 탈락한다.
   외부 지식이 섞일 때 가장 흔한 형태다.
4. **사람이 확정해야 쓰인다.** 생성 결과는 `draft`이고 `approved` 문항만 출제·QuizScore에 쓴다.
5. **절차 순서 문제는 모델 없이 만든다.** 원문 읽기 순서로 정답이 정해지므로 생성 오류가 없다.

외부 지식을 0%로 만들 수는 없다. 목표는 섞인 부분을 **기계적으로 찾아 걸러낼 수 있는 형태로
출력을 제한**하는 것이다.

## 전체 흐름

```text
업무(task) ─ task_document ─▶ 문서 검색 (질의: 업무명 + 설명, 현재 공개 색인)
        ▶ source_guard.screen_sources (제어 문형 차단, 기존)
        ▶ [절차 순서] Python이 연속 Block으로 생성
        ▶ [단답·객관식] Qwen generate_question → 별칭·인용 구간 JSON
        ▶ Quiz Guard (원문 대조 검증) ─ 탈락 사유 기록
        ▶ question (status=draft) ─ 사람 검토 ─▶ approved / rejected
        ▶ 인수자 답안 → Python 채점 → evaluation
        ▶ QuizScore → readiness(quiz_score=…)
```

## 문항 유형

| 유형 | 생성 | 정답 | Python 채점 (`python_score_mode`) |
|---|---|---|---|
| `ordering` 절차 순서 | Python. 같은 페이지 범위의 연속 Block 3~6개를 섞는다 | 원문 읽기 순서 | 인접 쌍 일치율 `맞은 인접 쌍 / (n−1)` (`rule_order`) |
| `short_answer` 단답 | 모델이 원문 한 구간을 빈칸으로 지정 | 원문 구간(+검토자가 승인한 동의 표현) | 정규화 후 완전 일치, 부분 점수 없음 (`rule_exact`) |
| `multiple_choice` 객관식 | 모델이 정답 구간과 오답 보기 3개 제안 | 원문 구간 | 정답 보기 선택 여부 (`rule_exact`) |

정규화는 공백·문장부호 제거와 전각/반각 통일이다. 기존 평가 도구의 `normalized`와 같은 규칙을
도메인으로 옮겨 함께 쓴다.

객관식 오답 보기는 정의상 원문에 없는 문장이므로 외부 지식이 들어올 수 있는 유일한 자리다.
"오답이 실제로는 맞는 말"인 경우를 막을 수 없으므로 객관식은 검토 화면에서 오답 보기를
반드시 확인하게 하고(`review_required_fields: ["distractors"]`), 단답·순서 문항을 우선 출제한다.

## Quiz Guard: 원문 대조 검증 (`domain/quiz_guard.py`, 순수 함수)

모든 항목을 통과해야 `draft`로 저장한다. 실패해도 생성 이력에는 문항과 사유를 남겨 평가에 쓴다.

| 코드 | 검사 |
|---|---|
| `schema_invalid` | Pydantic 형식 위반 (`extra="forbid"`) |
| `unknown_block` | 제공하지 않은 블록 별칭 |
| `quote_not_in_source` | 정답 인용 구간이 해당 Block 본문의 정규화 부분 문자열이 아님 |
| `ungrounded_token` | 문제·보기의 숫자, 날짜, 기간(`N년/개월/일`), 조항(`제N조`), `○○법·령·규칙`, 괄호 속 약어가 근거 Block에 없음 (오답 보기는 제외하고 별도 표시) |
| `answer_leak` | 정답 구간이 문제 문장에 그대로 들어 있음 |
| `distractor_in_source` | 오답 보기가 근거 원문에 들어 있음 (정답이 둘일 수 있음) |
| `duplicate_option` | 보기 중복, 정답 보기 수가 1이 아님 |
| `duplicate_question` | 같은 업무의 기존 문항과 정답 구간 또는 정규화 문제 문장이 같음 |
| `answer_too_long` | 단답 정답이 40자 초과 (단답이 아니라 문장 복사가 됨) |

토큰 추출 규칙은 버전(`quiz-guard-v1`)을 결과에 기록한다. 과하게 막는 오탐은 평가에서 측정한다.

## 모델 호출 계약

`ports/model_gateway.py`의 `generate_question`을 실제로 구현하되, 초안처럼 좁은 계약을 따로 둔다.

```python
class QuestionGateway(Protocol):
    model_id: str
    def validate_config(self) -> None: ...
    async def propose(self, task: dict, blocks: list[dict], kinds: list[str]) -> QuestionProposal: ...
```

모델 출력 (프롬프트 `quiz-gen-v1`, temperature 0):

```json
{"questions": [
  {"kind": "short_answer", "stem": "기록관리기준표의 기능분류는 몇 단계로 구성되는가?",
   "answer": {"block": "B3", "quote": "6단계"}, "evidence_blocks": ["B3"]},
  {"kind": "multiple_choice", "stem": "…",
   "answer": {"block": "B5", "quote": "…"}, "distractors": ["…", "…", "…"], "evidence_blocks": ["B5"]}
]}
```

- Block은 기존 Qwen 워커처럼 짧은 별칭(`B1…`)으로 제공하고 응답의 별칭을 실제 ID로 되돌린다.
- 워커(`qwen_worker.py`)에 `op: "question"`을 추가해 모델을 한 번만 띄운 상태로 재사용한다.
- 형식 오류 시 1회 재시도, 이후 `invalid_model_output`. 시간 초과는 기존 설정을 따른다.
- 생성 실행은 `generation_run`에 프롬프트·가드·검색 버전, 모델 ID, 어댑터, 입력 Block ID와 해시를 남긴다.

## 학습 파이프라인과의 호환 조건

학습(`train_pipeline.py`)은 입력 PDF 해시, 학습 설정, 초안 프롬프트 버전
(`LocalQwenDraftGateway.prompt_version`)으로 결과 재사용과 어댑터 호환을 판단한다. 문제 생성은
이 값을 바꾸지 않도록 다음을 지킨다.

- `qwen_worker.py`의 `selection_messages`·`relevance_messages`와 초안 `prompt_version`은 수정하지 않는다.
  문제 생성 프롬프트는 별도 함수·별도 버전(`quiz-gen-v1`)으로 둔다.
- 문제 생성은 어댑터 없이 기준 모델로 실행하고 사용한 모델 ID를 생성 이력에 남긴다. 현재 어댑터는
  원문 선택용으로만 학습되어 문제 생성 품질이 검증되지 않았다.
- 문제 생성용 학습은 별도 과제·평가를 학습 파이프라인에 추가하는 후속 작업이다.

## 저장 구조

기존 `question`·`evaluation` 테이블을 쓰고, 없는 열만 앱 시작 시 추가한다(업무 분석과 같은 방식).

| 테이블 | 추가 열 | 용도 |
|---|---|---|
| `question` | `status` (`draft`/`approved`/`rejected`/`retired`), `options_json`, `review_required_json`, `guard_json`, `generation_run_id`, `revision`, `reviewed_by`, `reviewed_at`, `review_note`. 동의 표현은 기존 `answer_json`에 둔다 | 검토 상태와 근거 검증 결과 |
| `evaluation` | 없음 (`answer`에 JSON, `python_score`는 100점 환산, `python_score_mode`, `status='scored'`) | 답안과 채점 |
| `task_document` | 없음 | 업무 ↔ 문서 연결. `load_workforce.py`가 `manual_references` 파일명(NFC 정규화)으로 등록된 문서를 찾아 채우고, 없는 문서는 이름을 출력한다 |

- `evidence_json`에는 Block ID와 함께 `document_id`, `ingestion_id`, 페이지 번호, 원문 해시를 저장한다.
  SQLite에 없는 Block은 저장을 거부한다.
  OCR 재처리 후에도 과거 문항의 근거를 그대로 추적한다.
- 확정 문항을 고칠 때는 덮어쓰지 않고 새 `question_id`로 만들고 이전 문항은 `retired`로 둔다.
  이미 제출된 답안이 어떤 문항 기준으로 채점됐는지 보존하기 위해서다.

## QuizScore와 준비도

```text
TaskQuizScore(e, t) = 100 × Σ 획득점수 / Σ 만점      (업무 t의 approved 문항, 문항별 최신 답안)
QuizScore(e)        = Σ Impact_t × TaskQuizScore / Σ Impact_t   (답안이 있는 업무만)
```

- 업무 영향도로 가중해 Readiness의 다른 두 구성 요소(CriticalTaskFit, TaskCoverage)와 관점을 맞춘다.
- Impact 80 이상 업무 중 답안이 없는 업무가 있으면 QuizScore를 `null`로 두고
  `critical_task_quiz_missing`을 돌려준다. 쉬운 업무만 풀어 준비도가 높아지는 것을 막는다.
- 정책 버전은 `assignment-v1` → `assignment-v2`로 올리고, 응답에 `quiz_coverage`(답안 업무 수/전체)를 함께 준다.

## API

| API | 내용 |
|---|---|
| `POST /api/v1/tasks/{task_id}/questions:generate` | 생성 작업 등록(비동기, 초안 생성과 같은 작업 방식) |
| `GET /api/v1/tasks/{task_id}/questions?status=` | 문항 목록과 Guard 결과, 근거 원문 링크 |
| `GET /api/v1/question-runs/{run_id}` | 생성 작업 상태, 통과/탈락 수와 사유 |
| `POST /api/v1/questions/{id}/review` | `approve`/`reject`, 동의 표현 추가, 메모 (버전 충돌 검사) |
| `GET /api/v1/employees/{id}/quiz` | 출제할 approved 문항 (정답 제외) |
| `POST /api/v1/employees/{id}/answers` | 답안 제출 → Python 채점 → evaluation 저장 |
| `GET /api/v1/employees/{id}/readiness` | 기존 API. QuizScore 반영 |

## 평가 (계획서 5.4.3)

`scripts/evaluate_questions.py`로 records-v1 업무 25개에 대해 생성하고 다음을 기록한다.

| 지표 | 정의 |
|---|---|
| 근거 존재율 | Guard 통과 문항 / 생성 문항 |
| 탈락 사유 분포 | 코드별 건수. 특히 `ungrounded_token` 비율 = 외부 지식 혼입 신호 |
| 정답 페이지 적중 | 근거 Block 페이지가 `manual_references` 페이지와 일치하는 비율 |
| 중복률 | `duplicate_question` / 생성 문항 |
| 사람 승인율 | approved / 검토 문항 (사용자 검토 후) |

`manual_references`의 인용문은 정답 데이터이므로 생성 입력에 넣지 않고 평가에만 쓴다.

## 구현 순서

| 단계 | 내용 | 완료 기준 |
|---|---|---|
| 1 | `domain/quiz.py`(Schema·채점·QuizScore), `domain/quiz_guard.py` | 순수 함수 테스트: Guard 코드별 통과/탈락, 채점 경계값, 핵심 업무 누락 시 null |
| 2 | 열 추가, `QuestionRepository`, `task_document` 연결 | 기존 DB 보존 마이그레이션 테스트, 재적재 시 같은 결과 |
| 3 | 절차 순서 문항(모델 없음) + 검토·답안·QuizScore API | 모델 없이 전체 흐름 동작: 생성→승인→답안→Readiness 값 |
| 4 | Qwen `op: "question"`, `QuestionGateway`, 프롬프트 `quiz-gen-v1` | 가짜 Gateway 테스트, 실모델 테스트(선택 실행) |
| 5 | 평가 스크립트와 records-v1 결과, `docs/quiz.md` | 지표 보고서, 탈락 사유 분석 |

3단계를 4단계보다 먼저 두는 이유는 모델 품질과 관계없이 흐름을 먼저 잇기 위해서다(2026-10-15 결정).

## 확정한 결정 (2026-10-19)

1. QuizScore는 업무 영향도 가중 평균이다(문항 단순 평균 아님).
2. Impact 80 이상 업무에 답안이 없으면 QuizScore는 `null`이고 `critical_task_quiz_missing`을 돌려준다.
3. 객관식은 v1에 포함한다. 오답 보기를 검토자가 확인해야 승인된다.
