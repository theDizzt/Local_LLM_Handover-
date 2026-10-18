# 업무 분석과 추천

2026-10-18 작성. [개발 계획서](plan/develop.md) 5.1.1의 업무 영향도·적합도·위험도·준비도 수식을
구현하고, 인수자별 추천·교육 대상·전체 분배안을 API로 제공한다. 정책 버전은 `assignment-v1`이다.
점수는 참고 지표이며 실제 배정과 인수인계 완료 판단은 담당자가 확정한다.

## 사용 방법

1. 업무·직원 데이터를 앱 DB에 넣는다. 다시 실행하면 같은 ID를 갱신한다.

   ```powershell
   .\.venv\Scripts\python.exe scripts\load_workforce.py
   ```

   기본 입력은 [가상 기록관리 업무 데이터](evaluation.md#가상-기록관리-업무-데이터-records-v1)
   (`datasets/records-v1/dataset.json`)이고 `--dataset`, `--database`로 바꿀 수 있다.
2. 서버를 실행하고 `http://127.0.0.1:8000/docs`의 `workforce`에서 호출한다.

| API | 내용 |
|---|---|
| `GET /api/v1/employees` | 인수자 목록 (인계자 제외) |
| `GET /api/v1/employees/{id}/recommendations?n=5` | 추천 업무 n개(1~20), 교육 대상, 추천 업무 총 소요 시간 |
| `GET /api/v1/employees/{id}/readiness` | 준비도와 구성 요소 |
| `GET /api/v1/assignments/plan` | 모든 업무를 인수자 한 명씩에게 나눈 분배안 |

## 수식

```text
Impact          = 0.32×중요도 + 0.24×실패영향 + 0.20×의존성 + 0.12×빈도 + 0.12×긴급성
SkillMatch      = 100 × Σ[r_k × min(p_k / r_k, 1)] / Σ[r_k]
Experience      = min(경력 개월 / 업무 기준 경력 개월, 1) × 100
Availability    = min(남은 월 가용 시간 / 업무 월 소요 시간, 1) × 100
Fit             = 0.55×SkillMatch + 0.30×Experience + 0.15×Availability
AssignmentScore = Impact × Fit / 100
RiskGap         = Impact × (1 − Fit / 100)
Readiness       = 0.50×QuizScore + 0.35×CriticalTaskFit + 0.15×TaskCoverage
```

- `CriticalTaskFit`: Impact 80 이상 업무의 평균 Fit. `TaskCoverage`: 전체 Impact 중 Fit 60 이상
  업무의 Impact 비율(%).
- 정렬은 AssignmentScore 내림차순, 같으면 task_id 오름차순이다.
- 필요한 값이 없거나 분모가 0이면 0으로 채우지 않고 `null`과 사유 코드를 돌려준다.

| 사유 코드 | 의미 |
|---|---|
| `required_skills_missing` | 업무에 요구 역량이 없음 |
| `experience_reference_missing` | 업무 기준 경력이 없거나 0, 또는 직원 경력이 없음 |
| `available_minutes_missing` | 직원 가용 시간이 없음 |
| `quiz_score_missing` | 문제 풀이 점수가 아직 없음 (문제 생성 기능 전에는 항상 해당) |
| `no_critical_tasks` | Impact 80 이상 업무가 없음 |

## 계획서에서 정한 해석

**Availability는 누적 방식이다.** 업무 하나(월 60~240분)와 월 가용 시간(1,200~2,400분)을 그대로
비교하면 모든 업무가 100이 되어 15% 가중치가 의미를 잃는다. 추천과 분배안에서는 앞서 고른 업무의
시간을 뺀 **남은 시간**으로 계산한다. 업무를 하나 고를 때마다 나머지 업무를 다시 계산한다.

**교육 대상(RiskGap)과 준비도는 전체 가용 시간 기준이다.** 일정이 아니라 역량 차이를 보는
지표이기 때문이다.

**분배안은 계획서 외 확장이다.** 계획서는 인수자별 순위만 정했지만 실제로는 업무 하나를 한
사람이 맡는다. 남은 업무와 인수자의 모든 조합에서 AssignmentScore가 가장 큰 조합부터
확정한다(탐욕법). 남은 시간이 업무 소요 시간보다 적은 인수자에게는 배정하지 않고, 누구에게도
배정할 수 없으면 `capacity_exceeded`로 남긴다. 최적해를 보장하지는 않지만 같은 입력이면 같은
결과가 나오고 배정 순서를 그대로 설명할 수 있다. Fit 60 미만 배정은 `low_fit_task_ids`로 표시한다.

인수자별 추천에서 다른 인수자의 Fit이 더 높은 업무에는 `better_fit_employee_id`를 붙인다.

## 가상 데이터 결과 (records-v1)

| 인수자 | 추천 1~3위 | 교육 1순위 | CriticalTaskFit / TaskCoverage |
|---|---|---|---|
| 김가온 (신규, 분류·평가) | TASK-001 단위과제 신설·변경, TASK-019 재난 피해 보고, TASK-002 보존기간 책정 | TASK-020 침수 응급복구 | 59.4 / 57% |
| 이도윤 (보존처리) | TASK-019, TASK-020, TASK-010 유해충 소독 | TASK-007 정보공개 청구 | 96.9 / 100% |
| 최하린 (시스템) | TASK-019, TASK-007 정보공개 청구, TASK-022 응급복구 공간 | TASK-015 습식 세척 | 76.7 / 83% |

분배안은 25개 업무를 모두 배정한다(이도윤 10개, 김가온 9개, 최하린 6개). 이도윤의 남은 시간이
30분까지 줄면서 복원처리 업무 TASK-014·015·016은 Fit 60 미만인 인수자에게 배정되어
`low_fit_task_ids`로 표시된다. 교육 또는 일정 조정이 필요한 업무가 드러나는 것이 의도한 동작이다.

가상 데이터의 총 소요 시간(3,650분)이 총 가용 시간(5,400분)보다 넉넉해 인수자별 추천 5개
안에서는 Availability가 대부분 100이다. 누적 효과는 추천 수를 늘리거나 분배안에서 나타난다.
준비도는 문제 풀이 점수가 없어 현재 모두 `null`이다.

## 코드와 검증

| 파일 | 역할 |
|---|---|
| `src/handover_ai/domain/assignment.py` | 수식·추천·교육 대상·준비도·분배 (순수 함수) |
| `src/handover_ai/adapters/workforce_repository.py` | `task`·`employee` 저장·조회 |
| `src/handover_ai/services/workforce.py`, `api/routes/workforce.py` | 응답 조립과 API |
| `scripts/load_workforce.py` | 정답 데이터 적재 |
| `tests/test_assignment.py` | 수식, 상한, 분모 0, 동점, 추천 개수, 누적 시간, 분배, API, DB 열 추가 |

기존 DB에는 `task.reference_experience_months`, `employee.role` 열이 없으므로 앱 시작 시
없는 열만 추가한다. 기존 데이터는 지우지 않는다.

## 아직 없는 것

- 화면. 현재는 API(`/docs`)로만 확인한다.
- QuizScore(문제 생성·채점)와 그에 따른 준비도 확정.
- 가중치와 임계값(80, 60)의 검증. 계획서의 초기 가설값이며 실험 후 조정한다.
