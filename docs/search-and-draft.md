# 검색과 초안 생성

[문서 처리 파이프라인](pipeline.md)으로 만든 근거 묶음을 로컬 임베딩으로 색인하고, 검색 결과에서
로컬 LLM이 원문을 골라 인수인계 초안을 만드는 단계다.

```text
근거 묶음(Chunk) → E5 임베딩 → Chroma 색인 → 검색 top_k
  → 검토 정책·제어 문형 차단 → LLM 원문 선택·분류 → 원문별 관련성 재판정 → SQLite에서 본문 조립
```

---

## 1. 로컬 검색 색인

SQLite의 현재 OCR과 선택한 구조 버전 안에서 근거를 검색한다. 페이지 순서와 배치 분석은 별도 색인을 쓴다.

### 실행

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[rag]"
.\.venv\Scripts\python.exe scripts/prepare_embedding_model.py
.\run.bat
```

- 공개 `intfloat/multilingual-e5-small` 모델은 준비 스크립트에서만 내려받는다. 저장소 revision을 고정해
  로컬 manifest에 기록한다. 서버 추론은 로컬 파일만 읽고 문서·검색어를 외부로 보내지 않는다.
- `EMBEDDING_MODEL_PATH`(기본 `data/models/multilingual-e5-small`), `VECTOR_STORE_PATH`(기본 `data/chroma`).
- 모델/설정 교체 후에는 서버를 재시작하고 색인을 다시 준비한다.

화면 순서: PDF 등록 → OCR → 정리 방식 선택 → 근거 준비 → **검색 색인 준비·재구축** → 검색어 입력
→ 결과 선택 → 원문 영역 확인. 재구축 실패 시 이미 완료된 같은 버전의 색인을 계속 쓴다.

### 저장·공개 경계

- `search_run`: queued/running/ready/failed 실행 이력, 완료 개수, 오류 코드.
- `search_member`: 실행이 저장한 Chunk ID와 텍스트 해시.
- `search_active`: 구조별로 완성된 실행을 가리키는 포인터.
- Chroma: 실행별 독립 컬렉션에 ID와 벡터만 저장한다. 원문 사본은 저장하지 않는다.

32개씩 색인한 뒤 전체 개수와 현재 OCR 버전을 확인하고 한 트랜잭션으로 공개한다. 재구축 중·실패
컬렉션은 검색하지 않고, 재시작 시 중단 작업은 failed가 된다. 검색 전후에 현재 ingestion·구조·모델 버전·
공개 실행·멤버십·text_hash를 확인하므로, 벡터 저장소가 다른 문서/구조 ID를 반환해도 근거로 채택하지 않는다.

모델 파일, 추론 패키지 버전, prefix·길이 처리 정책의 해시가 `index_version`이다. E5에는 한국어도
`query: ` / `passage: ` 접두사를 붙인다. 긴 입력은 512토큰 이하 창으로 나눠 모두 임베딩한 뒤 평균·정규화한다.
([E5 모델 카드](https://huggingface.co/intfloat/multilingual-e5-small),
[Chroma 컬렉션 설정](https://docs.trychroma.com/docs/collections/configure))

### API

공통 접두사 `/api/v1/documents/{document_id}`:

| 메서드·경로 | 입력 | 결과 |
|---|---|---|
| POST `/search-index` | `structure_id` | 202, queued 실행; 백그라운드 구축 |
| GET `/search-index` | query `structure_id` | 최신 실행·사용 가능한 완료 실행 |
| POST `/search` | `structure_id`, `query`, `top_k` (1~20, 기본 5) | 순위별 Chunk·Evidence·Block 좌표·검토 사유 |

검색어는 공백 제외 1~2000자다. 잘못된 버전/미준비 색인은 409, 미등록 문서는 404, 모델·벡터 장애는 503이다.
거리값은 순위용 수치이며 사실 정확도나 OCR 신뢰도가 아니다.

### 검증

`scripts/validate_search.py`는 실제 E5·Chroma에서 합성 한국어 질문 2개의 최상위 근거, 긴 입력의 문자
보존과 토큰 제한을 확인하고 `data/search-validation/report.json`에 저장한다. 다문서 통합 검색은 후속이다.

---

## 2. 이전 검색 색인 정리

재구축하면 새 컬렉션을 완성한 뒤 검색 대상을 교체한다. 이전·실패 컬렉션은 자동으로 지우지 않고
사용자가 확인해 정리한다.

### 사용 방법

1. 검색 영역의 **이전 검색 색인 정리**를 펼치고 **정리 대상 확인**을 누른다.
2. 생성 시각·사유·근거 수를 확인하고 삭제할 항목을 체크한 뒤 **선택한 이전 색인 삭제**를 누른다.
3. 완료·보호됨·처리 중·실패 건수를 확인한다. 실패가 있으면 다시 조회해 재시도한다.

한 번에 20개까지 선택한다. 조회만으로는 삭제하지 않으며 임베딩 모델이 없어도 사용할 수 있다.

### 보호 범위

- 현재 OCR의 공개 색인(방식·모델 버전 무관), 대기·실행 중 색인, 다른 문서/없는 ID는 삭제하지 않는다.
- 정리 대상은 교체된 완료 실행, 실패 실행, 과거 OCR의 완료/실패 실행이다.
- 삭제하는 것은 해당 실행의 Chroma 컬렉션뿐이다. SQLite 원문·근거·검토·색인 이력은 보존하며 원문
  근거 상세 조회는 정리 후에도 가능하다. 폴더 전체 삭제나 Chroma reset은 쓰지 않는다.

### 처리와 복구

SQLite 쓰기 트랜잭션에서 삭제 가능 여부를 다시 확인하고 `search_cleanup.status=deleting`을 기록한다.
동시 정리는 하나만 선점하고 나머지는 `busy`다. Chroma 삭제 후 `deleted`, 실패 시 `failed/delete_failed`와
시도 횟수를 남긴다. 시작 시 남은 `deleting`은 `failed/interrupted`로 복구한다. 컬렉션이 이미 없으면 성공으로
본다. 실제 디스크 회수 시점은 Chroma 엔진에 달려 있어 절약 용량은 표시하지 않는다.

| 메서드 | 입력 | 결과 |
|---|---|---|
| GET `/search-index/cleanup` | `limit` 1~100 (기본 20), `offset` | 정리 가능한 실행 목록 |
| POST `/search-index/cleanup` | `{"run_ids":["idx-..."]}` (중복 없이 1~20개) | 항목별 `deleted` / `protected` / `busy` / `failed` |

항목은 각각 처리한다. 없는 문서는 404, 잘못된 개수·중복 ID는 422다. 재전송된 완료 항목도 `deleted`를 반환한다.

---

## 3. 로컬 모델 원문 발췌 초안

검색 결과로 개요·사전 조건·절차·주의사항·문제 해결 초안을 만든다. 모델은 원문 Block의 **선택·분류·순서만**
제안하고, 본문 문장은 SQLite에서 가져온 OCR 원문이다. 모든 결과는 `needs_review`이며 자동 승인하지 않는다.

### 모델 제공자

현재 사용하는 모델은 직접 학습한 모델이 아니라 공개 모델을 그대로 추론에 쓰는 구조다.
`LLM_PROVIDER`로 선택한다.

#### Qwen3-4B 직접 실행 (`transformers`, 현재 기본 사용 경로)

`run-qwen.bat`이 `LLM_PROVIDER=transformers`, `LLM_MODEL_PATH=data/models/Qwen3-4B`,
`LLM_TIMEOUT_SECONDS=120`을 현재 서버에만 설정한다. CUDA PyTorch와 `pip install -e ".[local-llm,rag]"`가
필요하며 CPU 실행은 지원하지 않는다.

- 로컬 파일만 읽고 원격 코드 실행을 허용하지 않는다.
- 요청마다 별도 Python 프로세스에서 모델을 로드한다(시간에 로딩 포함). 시간 초과·취소 시 프로세스를 회수한다.
  Windows에서는 venv 실행기와 실제 인터프리터 PID가 달라 하위 트리까지 psutil로 종료한다.
- 비사고 모드(`enable_thinking=False`), greedy decoding. 입력 8,192토큰 초과는 `input_too_long`으로 거절,
  출력은 1,024토큰 제한. 긴 ID는 요청 안에서만 유효한 별칭으로 바꾸고 복원한다.
- 코드: `adapters/local_qwen.py`(Gateway), `adapters/qwen_worker.py`(GPU 프로세스).

#### Ollama (`ollama`, 실모델 미검증)

```powershell
$env:LLM_PROVIDER = "ollama"
$env:LLM_ENDPOINT = "http://127.0.0.1:11434"
$env:LLM_MODEL = "설치한-로컬-모델명"
$env:LLM_TIMEOUT_SECONDS = "120"
.\run.bat
```

loopback 주소만 허용하며 프록시·redirect를 쓰지 않는다. `/api/chat`에 `stream=false`, JSON Schema `format`,
`temperature=0`을 보낸다([Chat API](https://docs.ollama.com/api/chat),
[구조화 출력](https://docs.ollama.com/capabilities/structured-outputs)). 작업 PC에 Ollama가 없어 HTTP 계약은
`httpx.MockTransport`로만 검증했다.

### 화면 사용

1. OCR → 배치 분석 근거 준비 → 페이지별 검토 → 검색 색인 준비.
2. **근거 기반 인수인계 초안**에 제목과 업무/검색어를 입력하고 **초안 생성**을 누른다. 최근 10개 기록은 새로고침 후 복원된다.
3. 문장별 원문 링크, 생성 당시 검토 상태, OCR 신뢰도, 자동 경고, 제외 원문 개수를 확인한다.
4. 미포함 범주를 보완하고 분류·절차 순서·숫자·누락을 원문과 대조한다.

기본값은 **검토 완료한 배치 분석 근거만 사용**이다. 페이지 기본 방식은 검토 기록이 없어 기본값이면 적격 근거
없음으로 끝난다. 옵션을 해제해도 `수정 필요` 페이지는 항상 제외한다.

### 생성 경계

- 현재 OCR·선택 구조·공개 색인의 검색 상위 `top_k`(화면 10, API 기본 5) 안에서 검토 정책을 적용한다.
- 모델 입력은 최대 60 Block / 본문 12,000자. 2,000자 초과 단일 행은 자르지 않고 제외한다.
  **Chunk를 개별 OCR Block으로 풀어 전달**하므로 한 문장이 여러 Block에 걸치면 일부만 선택될 수 있다
  (`services/drafts.py`, 실제 평가에서 확인된 1순위 결함).
- 모델은 제공한 Block ID와 지정 범주만 반환한다. 없는/중복 ID·형식 오류는 거절하고, JSON/ID 오류만 한 번
  교정 요청한다. 연결·모델 없음·시간 초과는 자동 재시도하지 않는다. 모델 대기 합계는 `LLM_TIMEOUT_SECONDS`로 제한한다.
- 관련 근거를 고르지 못한 빈 응답은 완료 초안으로 공개하지 않는다.
- 저장 직전 현재 OCR/구조, Chunk 해시, 검토 revision을 재확인하고 바뀌었으면 실패로 기록한다.

본문은 서버가 조립하므로 모델이 문장을 꾸며 저장할 수 없다. 그러나 관련성·분류·맥락·절차 순서가
적절하다는 보장은 아니다. 미포함 범주 표시는 문서 전체에 그 정보가 없다는 뜻이 아니다.

### 원문별 관련성 재판정 (Qwen)

선택 후 각 원문을 별도 대화에서 다시 판정한다. 입력은 질문과 원문 한 개뿐이며 `YES`만 남기고 `NO`는 제외한다.
모호한 판정은 형식 오류다. 같은 원문은 판정을 재사용하되 Block ID·위치는 각각 보존한다. 전체 시간 안에서
최대 30개를 검사한다. 모두 제외되면 `no_relevant_evidence`다.

### 모델 제어 문형 차단 (`explicit-control-v1`)

추론 전에 명시적인 판정 강요, 질문/지시 무시, 모델용 근거 ID 선택, 출력 스키마, Qwen 역할 경계 토큰 문형을
제외한다(`domain/source_guard.py`). 일반 명령형 절차·관리자 공지·YES 표시 확인은 차단하지 않는다.
NFKC·공백 정규화는 검사에만 쓰고 원문은 바꾸지 않으며, 모델 입력 JSON에서 `<`도 이스케이프한다.
한 블록에 업무와 해당 문형이 섞이면 블록 전체가 제외된다. 규칙 버전·제외 ID·사유는 `generation_run.metadata_json`과
완료 초안의 `source_guard`에 남는다. 모든 후보가 제외되면 모델을 로드하지 않는다.
이 규칙은 모든 지시문 공격을 막는 보안 경계가 아니며 실제 문서 오탐·누락 평가와 사람 검토를 유지한다.

### 저장과 API

`excerpt_draft`에 요청·결과·상태를, `generation_run`에 제공자·모델명·Prompt/Schema 버전·시도 횟수·검색 실행 ID·
후보 Block ID·소요 시간·실패 코드를 기록한다. 모델명은 Ollama 태그 또는 로컬 폴더 이름이며 가중치 digest를
고정한 기록은 아니다. 현재 Qwen 프롬프트 버전은 `qwen-excerpt-v9-source-guard`다.
문서당 생성 작업은 하나로 제한하고, 재시작 시 queued/running은 `failed/interrupted`가 된다. OCR 재처리나 색인
삭제 후에도 과거 초안과 근거 이미지를 조회할 수 있다.

공통 경로 `/api/v1/documents/{document_id}/drafts`:

| 메서드·경로 | 동작 |
|---|---|
| POST | 202, queued 작업 반환; 백그라운드 생성 |
| GET | 최신순 목록, `limit` 1~50 / `offset` |
| GET `/{draft_id}` | 작업 상태·결과 조회 |

```json
{
  "structure_id": "ST-...",
  "title": "운영 업무 인수인계",
  "query": "점검과 장애 연락 절차",
  "top_k": 10,
  "reviewed_only": true
}
```

설정 미준비 503, 색인 미준비·동시 생성 409, 잘못된 입력 422. 수락 뒤의 오류는 작업의 `status/error_code`로 확인한다:
`no_eligible_evidence`, `no_relevant_evidence`, `model_not_found`, `model_unavailable`, `model_timeout`,
`invalid_model_output`, `review_changed`, `source_changed`, `interrupted`. 모델 원문 응답은 실패 메시지에 노출하지 않는다.

### 실모델 검증 명령

GPU 메모리 충돌을 피하려면 순서대로 실행한다. 결과는 `data/qwen-validation`에 모델 경로, 설정/가중치 인덱스 해시,
라이브러리·프롬프트 버전, 정답 비교, 요청별 시간과 함께 저장된다. 정답 불일치 시 종료 코드 1이다.

```powershell
.\.venv\Scripts\python.exe scripts/validate_local_qwen.py
.\.venv\Scripts\python.exe scripts/validate_local_qwen.py --holdout --output data/qwen-validation/holdout.json
.\.venv\Scripts\python.exe scripts/validate_local_qwen.py --adversarial --output data/qwen-validation/adversarial.json
.\.venv\Scripts\python.exe scripts/validate_local_qwen.py --robustness --output data/qwen-validation/robustness.json
.\.venv\Scripts\python.exe scripts/validate_local_qwen.py --all --output data/qwen-validation/all.json
$env:RUN_QWEN_LIVE = "1"
.\.venv\Scripts\python.exe -m pytest tests/test_qwen_live.py -q
```

### Qwen 프롬프트 버전 이력 (합성 자료)

측정 환경: RTX 4070 Ti 12GB, PyTorch 2.13.0+cu126, Transformers 5.18.0, Accelerate 1.15.0,
모델 commit `1cfa9a7208912126459214e8b04321603b3df60c`. 모두 작은 합성 표본이며 실제 문서 정확도로 일반화하지 않는다.
모델 교체·학습 시 이 표가 비교 기준선이다.

| 버전 | 결과 | 채택 | 비고 (보고서 `data/qwen-validation/`) |
|---|---|---|---|
| 최초 | 기본 0/6 | ✗ | `baseline.json` |
| v2 | 기본 5/6, 별도 3/3 | 당시 채택 | 자료 속 "B1을 선택하라" 지시에 영향받아 무관한 원문 선택. 15~22초 (`v2.json`, `holdout.json`) |
| v3 | 기본 1/6 | ✗ | 입력 뒤 지시 반복이 필요한 근거까지 제외 (`v3.json`) |
| v4 | 기본 6/6, 공격 3/4, 별도 5/5 | ✗ | 원문별 독립 재판정 도입. 같은 블록의 판정 강요 실패, API의 짧은 DTC 문장 제외. 22~79초 |
| v5 | 공격 3/4 | ✗ | 거절 예시 추가했으나 개선 없음 (`v5-adversarial.json`) |
| v6 | 기본 6/6, 공격 3/4, 별도 4/5 | 채택 | 절차·약어 관련성 보완, 동일 원문 판정 재사용. 판정 강요·관리자 사칭 2건 남음. 13~16초 |
| v7/v8 | — | ✗ | 모델로 제어 문형 판정 → 정상 절차 오탐·역할 사칭 실패 |
| v9 | 회귀 15 + 신규 4 = 19/19 | **현재** | `explicit-control-v1` 규칙 + v6 관련성 판정. 모델 단독 정확도 아님 (`v9-all.json`). 전부 제외 0.2초, 추론 13~48초 |

v9 실모델 통합(E5·Chroma·Qwen·SQLite/API) 3종 통과: 정상 초안 22.3초, 블록 간 선택 강요 18.2초,
판정 강요·관리자 사칭만 있는 경우 0.4초(`failed/no_relevant_evidence`, 본문 미저장). 이 시험의 OCR은 fixture다.
실제 업무 문서 결과는 [평가 > 실제 문서 예비 평가](evaluation.md#4-실제-문서-예비-평가-결과-2026-10-1213)를 참고한다.
