# 로컬 모델 자동 학습 시스템

2026-10-13 작성. 정답 없는 업무 PDF로 Qwen3-4B의 LoRA 어댑터를 학습·평가·등록한다.
[개발 계획](develop.md), [초안 생성](draft-generation.md),
[실제 문서 예비 평가 결과](real-evaluation-results-20261013.md)를 기준으로 설계했다.

## 사용 방법

1. 학습용 업무 PDF를 다음 폴더에 넣는다. 하위 폴더도 된다. 정답이나 질문은 필요 없다.

   ```text
   C:\Github\Local_LLM_Handover-\data\training-input\
   ```

2. 프로젝트 폴더의 `train.bat`을 실행한다. OCR → 합성 질문 → 학습 → 평가 → 판정까지
   자동으로 진행한다. 중간에 끊겨도 다시 실행하면 이어서 진행한다.
3. 결과가 **게이트 통과**이면 안내된 명령으로 적용한다. 실패이면 기존 모델을 그대로 쓴다.

| 명령 | 동작 |
|---|---|
| `train.bat` | 새 PDF가 있으면 학습. 중단된 실행이 있으면 이어서 진행 |
| `train.bat --force` | 새 PDF가 없어도 다시 학습 |
| `train.bat --status` | 학습 실행 목록·판정·현재 적용된 어댑터 |
| `train.bat --activate TR-…` | 통과한 어댑터를 적용. `run-qwen.bat` 재시작 후 반영 |
| `train.bat --deactivate` | 어댑터 해제, 기반 모델만 사용 |
| `train.bat --schedule 02:00` | 매일 지정 시각에 자동 실행 (Windows 작업 스케줄러) |
| `train.bat --unschedule` | 자동 실행 해제 |

주의할 점:

- **평가용 8개 PDF와 같은 파일은 자동으로 제외한다.** 평가 문서로 학습하면 평가 결과를 믿을 수 없다.
- 학습 중에는 화면에서 초안 생성을 하지 않는다. 같은 GPU를 쓴다. GPU 여유 메모리가
  6GB 미만이면 단계를 시작하지 않고, 메모리를 비운 뒤 다시 실행하면 이어서 진행한다.
- 첫 실행은 OCR(쪽당 약 10초)과 **기준 모델의 실제 문서 평가 1회**(약 30분)가 추가된다.
  기준 평가는 프롬프트 버전이 바뀌기 전까지 재사용한다.
- 학습 시간 대략값(RTX 4070 Ti): 합성 질문 묶음당 5~10초, 학습 예제 3,000개 × 2 epoch에
  1~3시간, 회귀·실제 문서 평가 약 40분.
- 학습 예제가 50개 미만이면 중단한다. 대략 **50쪽 이상**의 업무 PDF를 권장한다.
- 자동 실행 로그: `data/training-runs/<실행 ID>/pipeline.log`

## 1. 무엇을 학습하는가

### 1.1. 대상

기반 모델은 운영 경로의 **Qwen3-4B**(`data/models/Qwen3-4B`)로 고정한다. 기반 가중치는
바꾸지 않고 LoRA 어댑터 하나(`excerpt`)만 학습한다. 운영 Worker는 한 번 올린 모델로
다음 두 요청을 모두 처리하므로 어댑터도 두 작업을 함께 학습한다.

| 작업 | 입력 | 출력 |
|---|---|---|
| `select` 원문 선택 | 질문 + 별칭 ID Block 목록 | `{"items":[{"block_id","category"}]}` |
| `verify` 관련성 판정 | 질문 + 원문 1개 | `YES` / `NO` |

LLM A·B·C·D(구조화·문제 생성·채점·설명)는 같은 방식으로 나중에 붙인다. 채점(LLM C)은
[develop.md 3.8.2](develop.md)의 2인 독립 채점 Human Score가 생기기 전에는 시작하지 않는다.

### 1.2. 왜 이 작업부터인가

실제 문서 예비 평가에서 손실이 가장 큰 구간이 모델 단계다.

| 단계 | 정답 인용문 포함 |
|---|---:|
| OCR | 37 / 49 |
| 검색 상위 5개 | 21 / 49 |
| 모델 후보 | 12 / 49 |
| **최종 선택** | **2 / 49** |

후보에 있던 정답이 대부분 최종 선택에서 사라졌고(유답 32개 중 12개가 근거 없음),
DOC07-Q05에서는 질문 조건을 충족하지 않는 숫자를 골랐다.

### 1.3. 학습으로 해결하지 않는 것

| 결함 | 담당 |
|---|---|
| 문장·표 행이 OCR 줄 단위로 잘림 | 문단/표 행 근거 묶음 구조 버전 ([next-steps](next-steps.md) 1순위) |
| 검색 누락 | 검색·Chunk·후보 예산 개선 |
| 숫자 OCR 오류 | 사람 교정 revision. 모델이 값을 추정해 고치지 않는다 |
| 120초 시간 초과 | 요청마다 모델을 올리는 Worker 구조와 입력 길이 |
| 지시문 주입 | `explicit-control-v1` 규칙 유지. 학습은 보안 경계가 아니다 |

## 2. 전체 흐름

```mermaid
flowchart TD
    Input[data/training-input PDF] --> Discover[평가 PDF·중복·손상 파일 제외]
    Discover --> Prepare[1. prepare: OCR·Layout 구조·E5 색인]
    Prepare --> Synth[2. synth: 질문·정답 줄·무답 질문 생성과 필터]
    Synth --> Build[3. build: 운영 프롬프트 형식 데이터셋·표본 검토 파일]
    Build --> Train[4. train: 4bit QLoRA]
    Train --> Eval[5. eval: 회귀 19종 + 실제 문서 40문항]
    Eval --> Gate{6. gate: 기준 모델 대비 판정}
    Gate -->|통과| Candidate[data/models/adapters/실행ID 승격 후보]
    Gate -->|실패| Keep[기존 모델 유지]
    Candidate -->|train.bat --activate| Serve[run-qwen.bat이 어댑터 로드]
```

각 단계는 별도 프로세스로 실행해 단계가 끝날 때 GPU 메모리를 회수한다. 완료 단계는
`stages.json`에 기록하고 재실행 시 건너뛴다. 같은 작업 폴더를 두 학습이 동시에 쓰지 않도록
`train.lock`을 사용한다.

```text
data/training-input/            사용자가 넣는 PDF
data/training-work/             여러 실행이 공유하는 준비 결과
├─ training.db, chroma/, pages/ 학습 문서의 OCR·구조·색인 (앱 DB와 분리)
├─ synthetic/                   문서별 합성 기록 캐시 (생성 규칙 버전별)
├─ eval-baseline-<프롬프트>/      기준 모델 실제 문서 평가 (최초 1회)
└─ state.json                   마지막 완료 실행과 입력 지문
data/training-runs/TR-…/
├─ documents.json, fingerprint.json, stages.json, pipeline.log
├─ dataset/  train.jsonl, val.jsonl, data-card.json, review-sample.md
├─ adapter/  LoRA 가중치, training-meta.json
├─ eval/     regression.json, real/ (실제 문서 평가 전체)
└─ gate.json
data/models/adapters/TR-…/      게이트 통과 어댑터, active.txt
```

## 3. 데이터

### 3.1. 출처와 용도

| 출처 | 학습 | 평가 |
|---|:-:|:-:|
| `data/training-input`의 정답 없는 PDF → 합성 질문 | ✓ | 검증 손실만 |
| 실제 40문항 정답과 8개 평가 PDF | ✗ | ✓ 게이트 |
| Qwen 회귀 사례 19종 | ✗ | ✓ 게이트 |

40문항과 평가 PDF는 학습에 쓰지 않는다. 평가 PDF와 해시가 같은 입력은 자동 제외한다.
학습 PDF 안에서는 문서 단위로 검증 문서를 나눈다(3개 이상일 때 약 20%). 3개 미만이면
같은 문서 안에서 나누며 로그에 경고를 남긴다.

### 3.2. 합성 질문 생성 (`synth`)

1. Layout 구조의 같은 Chunk 안에서 연속된 OCR 줄을 최대 12줄·700자 묶음으로 만든다.
   절반씩 겹쳐서 묶음 경계에 걸린 문장도 다른 묶음에서 온전히 보이게 한다.
   문서당 묶음 수는 쪽수 기준 20~80개다.
2. 기반 Qwen3-4B(4bit, 어댑터 없음)가 줄 번호가 붙은 묶음을 보고 질문, 정답 줄 번호,
   범주, **무답 질문**(같은 주제지만 원문에 없는 연도·기관·수량 등을 묻는 질문)을 만든다.
   정답 줄은 여러 줄로 나뉜 절차를 빠짐없이 포함하도록 지시한다.
3. 필터: JSON·줄 번호·범주 오류, 원문 15자 이상을 그대로 복사한 질문, 정답 줄이 대부분
   제목 조각이거나 묶음의 70% 이상(또는 7줄 이상)을 차지하는 경우를 버린다.
   서식 PDF에서 제목 줄을 모두 정답으로 고르는 사례가 실제로 나와 추가한 규칙이다.
4. 무답 질문은 기반 모델의 관련성 판정이 정답 줄에 대해 `NO`일 때만 사용한다.
5. 후보 목록은 운영과 같은 함수로 만든다. E5 검색 상위 5개 → `eligible_sources`
   (60 Block/12,000자 제한) → `screen_sources`(제어 문형 차단). 정답이 후보 밖이면
   선택 예제를 만들지 않는다(검색 실패를 모델 탓으로 학습하지 않는다).

### 3.3. 학습 예제 (`build`)

| 예제 | 라벨 |
|---|---|
| 질문 + 후보 | 정답 줄(같은 문장의 반복 Block 포함)을 선택 |
| 무답 질문 + 그 후보 | 빈 선택 |
| 질문 + 정답 줄 하나 | YES (10자 미만 조각은 제외) |
| 무답 질문 + 정답 줄 하나 | NO |
| 질문 + 다른 문서의 줄 | NO |

작업·라벨 묶음별로 고르게 최대 3,000개를 뽑는다. 입력 메시지는 운영 Worker의
`selection_messages` / `relevance_messages`를 그대로 호출해 만든다. 프롬프트를 복제하지 않는다.
`review-sample.md`에 합성 기록 30개를 원문과 함께 남긴다. 합성 라벨에는 잡음이 있으므로
사람이 이 표본을 보고 오류가 많으면 생성 규칙(`GENERATOR_VERSION`)을 고친다.

## 4. 학습 (`train`)

| 항목 | 기본값 | 옵션 |
|---|---|---|
| 방식 | 4bit NF4 기반 + bf16 LoRA, gradient checkpointing | |
| LoRA | r=16, alpha=32, dropout 0.05, 모든 선형 층 | `--rank` |
| 학습률 | 1e-4, cosine, warmup 3% | `--learning-rate` |
| 배치 | 1 × gradient accumulation 16 | `--grad-accum` |
| 최대 길이 | 6,144토큰, 초과 예제 제외 | `--max-tokens` |
| Epoch | 2, 검증 손실이 가장 낮은 epoch 저장 | `--epochs` |

- 채팅 템플릿은 운영과 같은 `enable_thinking=False`, 손실은 답 토큰에만 계산한다.
- 답 위치의 logits만 계산한다(`logits_to_keep`). 전체 어휘 logits는 12GB에 들어가지 않는다.
- 메모리 부족 예제는 건너뛰고 수를 `training-meta.json`에 기록한다.
- `training-meta.json`의 `prompt_version`이 운영 Gateway와 다르면 어댑터를 로드하지 않는다.

## 5. 평가와 게이트 (`eval`, `gate`)

같은 평가를 기준 모델과 후보 어댑터에 실행한다. 기존 도구에 `--adapter` 옵션을 추가했다.

1. `scripts/validate_local_qwen.py --all --adapter …` (회귀 19종)
2. `scripts/evaluate_real_documents.py --adapter …` (40문항). 기존 평가 폴더
   `data/real-evaluation-20261012`의 OCR·색인을 복사해 605쪽 OCR을 다시 하지 않는다.

게이트 항목은 하나라도 실패하면 탈락이다.

| 항목 | 조건 |
|---|---|
| 회귀 19종 | 모두 통과 |
| 실제 문서 문항 | 기준과 같은 수를 모두 실행 |
| `false_answer` · `execution_error` · `missed_answer` | 기준 이하 |
| `selected_quote_exact` | 기준 이상 |
| 개선 | `missed_answer` 감소 또는 `selected_quote_exact` 증가 |
| 처리 시간 p95 | 기준의 1.2배 이하 |

평가 문항이 40개뿐이라 1~2문항 차이는 잡음일 수 있다. 따라서 통과해도 자동 적용하지 않고
**승격 후보**로만 등록한다. 사람이 `gate.json`과 `eval/real/report.html`을 확인한 뒤
`--activate`로 적용한다. 활성화·해제는 `active.txt` 한 줄만 바꾸며 파일을 지우지 않는다.
생성 이력의 모델 이름은 `Qwen3-4B+TR-…`로 남아 어떤 어댑터로 만든 초안인지 구분된다.

기준 평가와 후보 평가는 실행 시점의 GPU 여유 메모리가 다르면 처리 시간·시간 초과가 달라질 수
있다. 두 평가 모두 다른 GPU 프로그램을 끈 상태에서 실행하는 것이 공정하다.

## 6. 코드 위치

| 파일 | 역할 |
|---|---|
| `train.bat` | 패키지 확인·설치, 파이프라인 실행, 작업 스케줄러 등록 |
| `scripts/train_pipeline.py` | 단계 실행·재개·잠금·상태·활성화 명령 |
| `src/handover_ai/training/data.py` | 묶음·생성 결과 검사·학습 예제·분할 (순수 함수) |
| `src/handover_ai/training/model.py` | 4bit 생성기, QLoRA 학습 |
| `src/handover_ai/training/gate.py` | 게이트 판정 |
| `adapters/qwen_worker.py` | `selection_messages` 분리, 어댑터 로드 |
| `adapters/local_qwen.py` | 어댑터 경로·프롬프트 버전 검증 |
| `services/drafts.py` | 후보 규칙 `eligible_sources` 분리 |
| `run-qwen.bat` | `active.txt`가 있으면 `LLM_ADAPTER_PATH` 설정 |

## 7. 아직 하지 않은 것

- 초안 화면의 Block별 사람 피드백(유지·제외·추가·분류 변경) 수집과 학습 반영.
  피드백은 합성 데이터보다 정확한 라벨이므로 다음 확장 대상이다.
- 문단/표 행 근거 묶음 구조 버전. 이 구조가 생기면 학습 단위도 바뀌므로 다시 학습한다.
- 정답 위치(쪽·인용문·좌표) 기반 저장. 지금은 합성 기록이 Layout 구조의 Block ID를 쓴다.
- LLM A·B·C·D 역할 어댑터.
