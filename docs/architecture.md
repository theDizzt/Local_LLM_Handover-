# 아키텍처

전체 구조·계층 책임과 코드 읽는 순서.

## 목차

1. [시스템 아키텍처](#시스템-아키텍처)
2. [코드 읽기 안내](#코드-읽기-안내)

---

## 시스템 아키텍처

이 문서는 `docs/develop.md`의 개발 계획을 실제 코드 경계로 정리한 구현 아키텍처 이다.
외부 런타임보다 도메인 계약이 안쪽에 위치하는 Ports and Adapters 구조를 사용한다.

### 전체 흐름

```mermaid
flowchart TD
    PDF[스캔 PDF] --> Render[PyMuPDF 페이지 렌더링]
    Render --> OCR[PaddleOCR]
    OCR --> Layout[Layout 분석]
    Layout --> Extract[LLM A 문서 구조화]
    Extract --> Validate[Schema 및 근거 검증]
    Validate --> SQLite[(SQLite 기준 저장소)]
    SQLite --> Chunk[Chunk 생성]
    Chunk --> Embed[Embedding]
    Embed --> Chroma[(ChromaDB 검색 인덱스)]

    Request[문서 작성 요청] --> Search[SQL Filter 및 RAG 검색]
    SQLite --> Search
    Chroma --> Search
    Search --> Report[Gateway 보고서 생성]
    Report --> Validate

    Answer[인수자 답안] --> LLMEval[LLM C 채점]
    Answer --> PythonEval[Python 점수 엔진]
    LLMEval --> Compare[점수 및 Label 비교]
    PythonEval --> Compare
    Compare -->|정상 후보| Analysis[업무 영향도 및 준비도 분석]
    Compare -->|차이 또는 근거 오류| Review[LLM D 설명 및 사람 검토]
    Analysis --> API[FastAPI v1]
    Review --> API
    API --> UI[문서 검토 웹 화면 / 향후 React 및 Vite]
```

### 계층과 의존 방향

```
API → Services → Domain
         ↓
       Ports ← Adapters
```

| 계층 | 책임 | 현재 상태 |
|---|---|---|
| `api` | 버전 API, 요청 검증, 응답 변환 | Health·Architecture·점수 비교·문서 등록/조회·OCR 실행/상태/결과 구현 |
| `services` | 여러 도메인 규칙과 포트를 유스케이스로 조합 | 평가 비교·문서 등록·OCR 처리·페이지 구조/Chunk 생성 구현 |
| `domain` | 표준 JSON Schema, 점수 및 업무 분석 규칙 | 핵심 계약 구현 |
| `ports` | LLM·Repository·Vector Store 계약 | Protocol 구현 |
| `adapters` | SQLite·Ollama·ChromaDB·OCR 실제 연동 | SQLite 문서/OCR/Chunk Repository·PDF 검사/렌더링·PaddleOCR 구현 |
| `web` | 문서 입력 및 원문 검토 | HTML/CSS/JS 화면; OCR 검토·근거 준비/조회·원문 강조 |

도메인과 서비스는 Ollama, ChromaDB, PaddleOCR의 응답 객체를 직접 참조하지 않는다.
외부 결과는 어댑터가 표준 Schema로 변환한다.

### 저장소 책임

SQLite는 문서와 OCR 원문, 계층 구조, Chunk 메타데이터, 근거 연결, 생성 실행 이력의 기준 저장소이다.
ChromaDB는 재구축할 수 있는 검색 인덱스이며 기준 데이터로 사용하지 않는다.

```
Local File Store
├─ 원본 PDF
└─ 렌더링한 페이지 이미지

SQLite
├─ Ingestion / Document / Page
├─ Section / DocumentBlock
├─ Chunk / ChunkSource
└─ GenerationRun

ChromaDB
└─ 실행별 Chunk ID + Embedding (원문·공개 여부는 SQLite에서 검증)
```

문서 변경 또는 OCR 재실행 시 새로운 `ingestion_id`를 만든다.
인덱스는 `pending → ready` 상태가 된 버전만 검색에 노출하고 실패한 인덱스는 SQLite와 원본 파일에서 재구축한다.

### LLM 역할과 Gateway

논리 역할은 최대 네 개지만 같은 로컬 모델을 역할별 설정으로 재사용할 수 있다.

| 역할 | Gateway 메서드 | 출력 |
|---|---|---|
| 문서 구조화 | `extract_document` | 구조화된 업무 및 문서 정보 |
| 문제 생성 | `generate_question` | 문제·정답·Rubric·근거 |
| 답안 평가 | `evaluate_answer` | 기준별 점수와 답안 근거 |
| 차이 설명 | `explain_difference` | 점수 차이 설명과 검토 정보 |

보고서 생성은 기존 Gateway의 `generate_report`를 사용하며 별도 다섯 번째 모델을 요구하지 않는다.
Provider 호출 전후의 모델 ID, Prompt·Schema 버전, 검색 근거 ID, 처리 시간과 결과 상태를 `generation_run`에 남긴다.

### 검증 경계

- LLM 출력은 Pydantic 검증 전까지 신뢰하지 않는다.
- 인수인계서의 모든 내용은 등록된 `evidence_id`를 참조해야 한다.
- LLM이 만든 SQL은 실행하지 않고 미리 정의된 Query만 매개변수화해 실행한다.
- 두 점수가 모두 있고 Label이 같으며 점수 차이가 5 이하인 경우만 자동 처리 후보로 둔다.
- 자동 처리 후보는 실제 인수인계 완료 승인이 아니며 담당자의 최종 판단을 대체하지 않는다.

### 단계별 구현 순서

1. [완료] SQLite 문서 Repository와 문서 등록 트랜잭션, 목록·상세 API를 구현한다.
2. [구현] PyMuPDF·PaddleOCR 어댑터, 작업 상태/재처리 API와 합성 스캔 Fixture를 연결했다.
   OCR 결과는 페이지·좌표·신뢰도와 함께 저장하며 Layout 분석은 후속 작업이다.
3. [기본 구현] 페이지별 Section·Chunk·원문 근거 연결과 검토 화면을 구현했다.
   페이지 fallback과 좌표 규칙 기반 Layout 분석을 별도 버전으로 제공한다.
   제목/표/다단은 검토가 필요한 제안이며 셀 복원과 실제 문서 품질 평가는 후속이다.
   ChromaDB 색인 상태 전이·구조별 검색·원문 이동도 구현했다. 상세는
   [검색 색인](search-and-draft.md#로컬-검색-색인)을 참고한다.
4. [발췌 초안 구현] Ollama Gateway·JSON Schema·제한 재시도와 생성 이력을 연결했다.
   원문 ID를 선택하여 본문을 서버에서 조립한다. 실모델 추론 검증과 다른 역할 확장은 후속이다.
5. RAG 보고서 생성, 문제 생성, 답안 평가를 순서대로 연결한다.
6. 준비도·추천 계산과 React UI를 추가한다.
   문서 등록·OCR 원문 검토용 최소 화면은 먼저 구현했다. 전체 작성 UI는 후속 작업이다.
7. 장애 주입, 모델 교체, 근거 추적 전체 흐름을 Golden Test로 검증한다.

구조 버전은 `structure_run`과 `structure_section`으로 관리한다. 기존 원문 Block을
덮어쓰지 않고 과거 근거를 보존한다. 상세 규칙은 [문서 구조](pipeline.md#문서-구조chunk원문-근거-연결),
후속 작업별 완료 기준은 [다음 작업 계획](roadmap.md)을 참고한다.

---

## 코드 읽기 안내

처음에는 아래 순서로 읽으면 됩니다. 소스의 `[읽기 안내]`와 함수 내부 `#` 주석은
학습용 설명이며 읽은 뒤 삭제해도 실행 동작에는 영향이 없습니다. 기존 docstring은
일부 API 설명에도 사용되므로 필요에 따라 남겨 두세요.

### 1. 전체 구조

`main.py → api/routes → services → domain + ports → adapters`

- `main.py`: 앱을 만들고 라우터와 시작 작업을 등록합니다.
- `config.py`, `api/dependencies.py`: 환경 설정과 실제 구현 객체를 조립합니다.
- `api/routes`: HTTP 요청/응답과 상태 코드를 다룹니다.
- `services`: 등록·OCR·평가처럼 한 번의 작업 순서를 조정합니다.
- `domain`: 데이터 형식, 검증 조건, 점수 계산 같은 업무 규칙입니다.
- `ports`: 서비스가 외부 기술에 요구하는 메서드 계약입니다.
- `adapters`: SQLite·PyMuPDF·PaddleOCR로 그 계약을 구현합니다.

### 2. PDF 등록 따라 읽기

1. `api/routes/documents.py`의 `register_document`: 업로드 파일을 받습니다.
2. `services/documents.py`의 `register`: 임시 파일 복사, 크기/해시 계산을 합니다.
3. `adapters/pdf.py`: PDF를 실제로 열어 유효성과 페이지 수를 확인합니다.
4. `adapters/document_repository.py`: 중복 조회와 메타데이터 저장을 합니다.
5. `adapters/sqlite.py`의 `connect`: 성공 시 commit, 실패 시 rollback합니다.
6. 서비스의 `finally`: 임시 파일과 실패한 요청의 원본을 정리합니다.

`tests/test_documents.py`를 나란히 보면 각 분기가 어떤 상황에서 실행되는지 확인할 수 있습니다.

### 3. OCR 따라 읽기

1. `api/routes/ocr.py`: 요청을 받아 202와 작업 ID를 반환합니다.
2. `services/ocr.py`의 `enqueue`: 새 작업과 처리 버전을 DB에 만듭니다.
3. 응답 뒤 `BackgroundTasks`가 같은 서비스의 `run`을 호출합니다.
4. `adapters/ocr.py`: PDF 페이지를 PNG로 바꾸고 OCR 결과를 표준 좌표로 변환합니다.
5. `adapters/ocr_repository.py`: 페이지·텍스트·진행률을 저장합니다.
6. 전체 성공 시 `complete`가 문서의 현재 처리 버전을 바꿉니다.
7. 실패 시 `fail`은 새 작업만 실패로 기록하고 이전 성공 결과를 유지합니다.

원본 파일 ID인 `document_id`, 처리 버전인 `ingestion_id`, 진행 상태 조회용 `job_id`는
서로 역할이 다릅니다. `domain/ocr.py`의 주석에서 좌표와 상태 표현을 확인하세요.

### 4. 평가 계산 따라 읽기

`api/routes/evaluations.py → services/evaluation.py → domain/scoring.py`

현재는 두 경로에서 전달받은 기준별 점수를 환산하고 비교합니다. LLM 자동 채점은 아직
연결되지 않았습니다. `None`은 0점이 아니라 계산 불가, `accepted_candidate`는 최종 승인이
아니라 자동 처리 후보입니다. `tests/test_scoring.py`의 작은 숫자 예제부터 읽으면 편합니다.

### 자주 나오는 문법

| 표현 | 이 프로젝트에서의 의미 |
|---|---|
| `Protocol` | 구현체가 제공해야 할 메서드 목록 |
| `BaseModel`, `Field` | 데이터 형식과 입력 제약 검증 |
| `Annotated[..., Depends(...)]` | FastAPI가 필요한 객체를 준비해 인자로 전달 |
| `with` | 파일/DB 연결을 정해진 범위에서 사용하고 정리 |
| `yield` | 연결을 with 본문에 전달하거나 페이지를 하나씩 생성 |
| `try/except/finally` | 처리 시도, 오류 대응, 성공 여부와 무관한 정리 |
| SQL의 `?` | 문자열 조립 대신 값을 안전하게 바인딩하는 자리 |
| `rowcount` | 조건부 UPDATE에 실제로 영향을 받은 행 수 |
| `@lru_cache` | 같은 인자의 객체 생성 결과 재사용 |
