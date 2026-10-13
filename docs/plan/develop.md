# Local AI 기반 인수인계서 작성 및 분석 시스템 개발 계획서

## 1. 프로젝트 개요 및 개발 방향

### 1.1. 프로젝트 개요

본 프로젝트는 기업 또는 조직 내부의 **스캔된 PDF 형태의 업무 인수인계 자료**를 Local AI가 분석하여 업무 정보를 구조화하고 이를 기반으로 새로운 인수인계서를 작성하며 인수자의 업무 이해도와 업무 수행 적합성을 평가하는 시스템을 개발하는 것을 목표로 한다.

단순한 문서 생성 기능에 그치지 않고 다음 기능을 하나의 흐름으로 연결한다.

```
스캔 PDF 입력
   ↓
OCR 및 문서 구조 분석
   ↓
업무 정보 구조화
   ↓
정형 / 비정형 데이터 저장
   ↓
RAG 기반 인수인계서 작성
   ↓
문제 자동 생성
   ↓
인수자 답변 평가
   ↓
LLM + Python 점수 분석
   ↓
업무 영향도 / 적합도 분석
   ↓
인수인계 준비도 및 추천 업무 출력
```

프로젝트의 최종 목표는 **문서를 대신 써주는 AI가 아닌 인수인계 자료를 분석하고 업무 전달 상태까지 평가하는 시스템**을 구축하는 것이다.


### 1.2. 최종 개발 방향

1차 개발에서는 입력 문서를 **스캔 PDF로 제한**한다.

기존 개발안에서는 PDF, DOCX, XLSX 등을 모두 입력 대상으로 고려했지만, 이번 개발에서는 가장 처리 난도가 높은 스캔 PDF를 우선적으로 해결한다.

```
1차
Image-based PDF
→ OCR
→ 분석

2차
Text PDF

3차
DOCX / XLSX / TXT 등
```

처음부터 너무 많은 Parser를 구현하는 문제를 피하면서 프로젝트의 핵심 기술인 **OCR, 문서 구조 분석, RAG**에 집중할 수 있게 한다.

---

## 2. PDF 파일 처리

### 2.1. 스캔 PDF 처리 구조

스캔 PDF는 내부에 텍스트가 존재하지 않거나 신뢰하기 어려우므로 다음과 같은 파이프라인을 사용한다.

```
Scanned PDF
     ↓
PyMuPDF: 페이지 단위 Image Rendering
     ↓
PaddleOCR
     ↓
텍스트 + Bounding Box
     ↓
문서 Layout 분석
     ↓
제목 / 목차 / 표 / 본문 판별
     ↓
문서 구조화
```

#### 2.1.1. PyMuPDF의 역할

- PDF 페이지 수 확인
- 페이지별 이미지 렌더링
- 원본 페이지 번호 유지
- 페이지 이미지 저장
- OCR 결과와 원본 페이지 연결

#### 2.1.2. PaddleOCR의 역할

```
OCR Text
Bounding Box
OCR Confidence
Page Number
```

단순히 문자열만 저장하지 않고 **어느 페이지의 어느 위치에서 나온 문장인지 함께 보존**한다.

### 2.2. PDF 문서 구조 분석

핵심 연구 요소 중 하나는 **PDF의 목차, 제목, 소제목 같은 문서 구조를 DB에 어떻게 저장할 것인가**이다.

#### 2.2.1. Document 테이블

```
document_id
file_name
page_count
created_at
file_sha256
source_uri
document_version
ingestion_id
ocr_version
parser_version
```

#### 2.2.2. Section 테이블

```text
section_id
document_id
parent_section_id
title
level
page_start
page_end
section_path
title_source
structure_confidence
```

예시:
```
section_id: S021
title: CAN 오류
level: 2
parent_section_id: S020
section_path: 2 > 장애 처리 > CAN 오류
page_start: 12
page_end: 14
```

`parent_section_id`를 이용해 문서 계층 구조를 저장한다.

`title_source`는 `toc`, `body_heading`, `inferred`로 구분한다. 목차와 본문 제목이 충돌하면 원문을 모두 보존하고 확인 상태를 기록한다. `page_start`, `page_end`는 1부터 시작하는 실제 PDF 페이지 번호이며 인쇄된 페이지 표기와 구별한다. 같은 문서 안의 부모만 참조하도록 외래 키 및 계층 순환 검사를 적용한다.

```
장애 처리
   ├─ CAN 오류
   └─ Sensor 오류
```

#### 2.2.3. Page와 DocumentBlock 저장 규격

| 대상 | 필수 필드 및 규칙 |
|---|---|
| Page | `page_id`, `document_id`, `ingestion_id`, `pdf_page_number`, `printed_page_label`, `image_uri`, `width_px`, `height_px`, `render_dpi`, `rotation` |
| DocumentBlock | `block_id`, `page_id`, `section_id`, `block_type`, `reading_order`, `text`, `bbox`, `ocr_confidence`, `structure_confidence` |
| 구조 유형 | `heading`, `paragraph`, `table`, `figure`, `list`, `footnote`, `header`, `footer`, `toc` |
| 표 | `table_group_id`로 여러 페이지에 걸친 표를 연결하고 행·열·병합 셀과 원문 block 참조를 저장 |
| 좌표 | 회전 보정 후 저장된 페이지 이미지의 왼쪽 위를 원점으로 하는 `[x0, y0, x1, y1]`, 각 값은 0~1 정규화 |

인쇄 페이지 표기는 로마 숫자나 누락을 허용하는 문자열이다. 다단 문서는 `reading_order`로 읽기 순서를 보존한다. confidence는 0~1이며 원본 OCR 점수와 구조 분석 점수를 혼합하지 않는다. OCR을 재실행하면 새 `ingestion_id`를 발급하여 기존 결과와 근거 참조를 유지한다.

---

## 3. RAG 및 DB 구조

### 3.1. Chunk 데이터 구조

RAG에서 실제 검색하는 단위는 별도의 Chunk로 관리한다.

```
chunk_id
document_id
section_id
page_number
chunk_order
text
content_type
ocr_confidence
bbox
embedding_id
```

예시:
```
chunk_id: CH-00231
section_id: S021
page_number: 12
content_type: procedure
text: CAN 오류 발생 시 DTC를 우선 확인한다.
ocr_confidence: 0.94
```

이를 통해 검색 결과에서 원본 PDF 페이지와 위치까지 역추적할 수 있다. Chunk에는 `ingestion_id`, `chunking_version`, `text_hash`를 추가한다. 여러 block을 합친 Chunk는 `ChunkSource(chunk_id, block_id, source_order)`로 모든 원본 위치를 연결한다. 위 `page_number`, `bbox`는 단일 영역 예시이며 여러 페이지 · 영역의 근거를 하나의 `bbox`로 축약하지 않는다. `content_type`은 `procedure` 같은 의미 분류이고 원본의 시각적 유형인 `block_type`과 구분한다.

### 3.2. 정형·비정형 혼합 데이터베이스 구조

#### 3.2.1. 정형 데이터

SQLite 를 사용한다.

```
Task
Employee
Question
Evaluation
Score
Document
Section
Chunk Metadata
```

#### 3.2.2. 비정형 / 의미 검색 데이터

Vector Store 를 사용한다.

```
OCR Text Chunk
Embedding
Semantic Search
RAG 검색용 데이터
```

#### 3.2.3. 구조 예시

```
SQLite
   ├─ Task
   ├─ Employee
   ├─ Score
   ├─ Document Structure
   └─ Chunk Metadata

ChromaDB
   ├─ Chunk Text
   ├─ Embedding
   └─ Metadata Reference
```

두 저장소는 `chunk_id`, `document_id`로 연결한다.

### 3.3.3. 원본 데이터와 검색 인덱스의 책임

SQLite를 업무 데이터, OCR 원문, Chunk, 근거 참조, 생성 문서 및 버전의 기준 저장소로 사용한다. ChromaDB의 텍스트 · 메타데이터는 검색용 복제본이며 SQLite와 원본 파일로 재구축할 수 있어야 한다. 원본 PDF와 페이지 이미지는 로컬 파일 저장소에 두고 DB에는 URI와 hash를 저장한다.

`index_status`를 `pending`, `ready`, `failed`로 관리한다. DB 저장 후 색인을 생성하고 성공한 버전만 검색에 노출한다. 재시도는 같은 Chunk/색인 버전에 대해 중복 없이 수행한다. 문서 갱신 및 삭제 시 관련 색인을 무효화하고 검색 후에도 DB에서 유효성을 확인한다. Embedding 모델, 차원, 정규화 방식, 버전이 바뀌면 새 색인을 구축하여 전환하며 서로 다른 모델의 벡터를 섞지 않는다.

### 3.4. RAG 기반 인수인계서 작성

```
사용자 요청
    ↓
Query 생성
    ↓
Embedding
    ↓
Vector Search
    ↓
관련 Chunk 검색
    ↓
Source Metadata 조회
    ↓
LLM
    ↓
Structured Output
    ↓
인수인계서
```

LLM에 PDF 전체를 전달하지 않고 관련 Chunk만 전달해 Context 사용량을 줄이고 생성 결과의 근거성을 높인다.

#### 3.4.1. SQL 조회와 RAG 적용 범위

| 요청 | 처리 경로 |
|---|---|
| 업무 ID, 담당자, 점수, 조건별 업무 목록 | 허용된 SQL/Repository 조회 |
| 인수인계서 작성, 문서 기반 문제 생성 | 관련 근거 검색 후 생성하는 RAG |
| 유사 장애 사례와 관련 문단 검색 | Vector Search, 필요하면 FTS 키워드 검색 결합 |

RAG는 근거를 검색하여 생성에 사용하는 흐름이다. 모든 조회를 Vector DB로 수행한다는 의미는 아니다. 생성 전에는 문서 · 버전 필터, `top_k`, 중복 제거, 토큰 예산을 적용하고 검색된 `chunk_id`를 기록한다. 근거가 없거나 출처가 충돌하면 내용을 추측하지 않고 `needs_review`와 누락 · 충돌 항목을 반환한다. Vector Store 장애 시 FTS 검색으로 대체할 수 있지만 실제 근거가 확보된 경우에만 생성한다.

#### 3.4.2. 인수인계서 표준 출력 데이터

최종 인수인계서는 아래 공통 JSON을 검증한 뒤 Jinja2로 렌더링한다. 화면에는 검증된 최종 결과와 출처 · 확인 필요 상태를 표시한다. 모델의 내부 추론이나 미검증 원시 응답을 최종 문서에 넣지 않는다.

```json
{
  "schema_version": "1.0",
  "handover_id": "HO-001",
  "revision": 1,
  "title": "CAN 장애 대응 인수인계",
  "status": "needs_review",
  "owner_employee_id": null,
  "recipient_employee_id": "EMP-003",
  "tasks": [{
    "task_id": "TASK-021",
    "overview": {"text": "CAN 오류 발생 시 진단과 복구를 수행한다.", "evidence_ids": ["EV-001"]},
    "prerequisites": [],
    "steps": [{"order": 1, "text": "DTC를 확인한다.", "evidence_ids": ["EV-001"]}],
    "cautions": [],
    "troubleshooting": []
  }],
  "evidence": [{
    "evidence_id": "EV-001",
    "document_id": "DOC-001",
    "document_version": 1,
    "ingestion_id": "ING-001",
    "chunk_id": "CH-00231",
    "block_ids": ["BL-001"],
    "pdf_page_number": 12
  }],
  "missing_fields": ["owner_employee_id", "tasks[0].prerequisites"],
  "conflicts": [],
  "generation_run_id": "RUN-001",
  "template_version": "1.0"
}
```

`overview`, `prerequisites`, `steps`, `cautions`, `troubleshooting`의 각 내용 항목은 `text`와 `evidence_ids`를 가진다. 절차 항목에는 `order`를 추가한다.

필수 키는 모두 유지하며 모르는 값은 `null` 또는 빈 배열과 `missing_fields`로 표현한다. 담당자 ID는 Employee 참조를 검증하고 사용자 입력인지 문서 추출인지 출처를 기록한다. 각 내용 항목의 근거는 같은 처리 버전의 유효한 Chunk/Block이어야 한다. Schema 검증과 참조 검증에 통과한 경우만 저장 · 렌더링하고 필수 정보 누락이나 충돌이 있으면 검토 필요 표시를 유지한다. `generation_run_id`는 모델, 프롬프트, 검색 설정, 생성 시각 이력과 연결한다.

### 3.5. LLM 종속성 제거

시스템은 특정 모델에 종속되지 않도록 `Model Gateway`를 둔다.

```
Application
     ↓
LLM Gateway
     ↓
┌────────┬────────┬────────┐
│ Ollama │ llama  │ 향후   │
│        │ .cpp   │ Runtime│
└────────┴────────┴────────┘
```

공통 Interface 예시:
```
generate_question()
evaluate_answer()
extract_document()
generate_report()
```

모델이 바뀌어도 Backend 로직이 유지되도록 한다.

#### 3.5.1. 교체 가능한 계층의 계약

| 계층 | 공통 계약 | 교체 시 확인할 사항 |
|---|---|---|
| Frontend | 버전이 있는 FastAPI 요청·응답 Schema | UI 교체 후 동일 API 계약 유지 |
| Structured DB | `DocumentRepository`, `TaskRepository`, `EvaluationRepository` | ID · 외래 키 · 트랜잭션 · 마이그레이션 · 정렬 결과 유지 |
| Vector Store | `upsert`, `search`, `delete` 검색 인터페이스 | 문서 필터·근거 ID · 색인 버전 유지 |
| LLM | 역할별 요청/응답 Schema를 가진 Gateway | Schema 지원, timeout, 오류 형식, 품질 기준 통과 |

도메인 로직은 SQLite/ChromaDB/Ollama 전용 응답을 직접 사용하지 않는다. Provider와 모델 ID, endpoint, timeout, context 한도는 설정으로 주입한다. 모든 Provider 호출은 로컬 환경에서 동작하도록 구성하고 교체 시 같은 테스트 데이터로 계약 검증을 수행한다.

### 3.6. 모델이 달라도 유사한 결과를 내기 위한 설계

목표를 **같은 자연어 문장 생성**이 아니라 **같은 구조화 데이터 생성**으로 정의한다.

```json
{
  "task_id": "TASK-021",
  "problem_type": "CAN_ERROR",
  "priority": 90,
  "required_skill": ["CAN", "STM32"]
}
```

```
LLM A ─┐
       ├→ Standard JSON → Template → 최종 결과
LLM B ─┘
```

사용 기술:

- Pydantic
- JSON Schema
- Prompt Template
- Model Adapter
- Jinja2

#### 3.6.1. 결과 동등성의 정의

공통 JSON 형식만으로 모델 간 내용 일치가 보장되지는 않는다. 확정된 정형 데이터의 조회, 수식 계산, 템플릿 렌더링은 같은 입력과 버전에서 같은 결과를 요구한다. 문서 추출, 문제 생성, 자유 답안 평가는 검증 데이터에 대한 정확도, 판정 일치율, 점수 오차로 동등성을 평가한다. 자연어 문장 자체의 완전 일치는 요구하지 않는다.

Unicode, 공백, 단위, 날짜, enum 표기를 정규화하고 집합형 배열만 정렬한다. 절차처럼 순서가 의미를 갖는 배열은 유지한다. 점수는 내부 정밀도로 계산한 뒤 표시할 때 소수점 둘째 자리에서 반올림하여 한 자리까지 출력한다. 순위 동점은 `task_id` 오름차순으로 결정한다.

#### 3.6.2. 실행 이력과 모델 교체 검증

각 실행은 원문/답안 hash, 문서 · OCR · Chunk · 검색 버전, 검색된 근거 ID, Prompt · Schema · Rubric · 계산 규칙 · Template 버전, 모델 ID와 양자화, runtime 버전, temperature, 지원 시 seed, 재시도 횟수, 처리 시간 및 결과 상태를 기록한다. temperature와 seed 고정만으로 완전 재현성을 보장한다고 가정하지 않는다.

| 초기 수용 기준 | 검증 방법 |
|---|---|
| 최종 저장 결과의 Schema · 참조 유효성 100% | 실패 응답은 정상 결과에서 제외하고 실패율을 별도 보고 |
| 동일 정형 입력의 Python 계산 · Label 일치 100% | 경계값, 동점, 결측값을 포함한 Golden Test |
| 모델 원시 응답 Schema 성공률 95% 이상 | 최소 30개 고정 사례를 모델별 3회 실행, 재시도 전후 구분 |
| 필수 추출 필드 정답 일치율 95% 이상 | 사람이 확정한 필드값과 비교 |
| 평가 Label 일치율 95% 이상, 점수 MAE 5점 이하 | 같은 답안, Rubric, 근거를 고정하고 100점 척도에서 비교 |

마지막 기준은 모델 간 비교와 Human Score 대비 비교를 각각 보고한다. 탈락 및 검토 필요 사례도 분모와 상태 통계에 포함하여 성공 사례만으로 품질을 높여 보이지 않게 한다. 모델 변경과 OCR/검색 변경을 별도 실험으로 분리한 후 전체 파이프라인을 확인한다. 기준을 충족하지 못하면 기존 모델을 유지하거나 사람 검토 대상으로 처리한다.

### 3.7. LLM 최대 4개 역할 분리

#### 3.7.1. LLM A: 문서 구조화

```
OCR 결과
↓
업무 정보 추출
↓
Structured JSON
```

#### 3.7.2. LLM B: 문제 생성

```
인수인계 문서
↓
문제, 정답, 난이도, Rubric, Evidence
```

#### 3.7.3. LLM C: 답안 평가 + 점수 계산

```json
{
  "criterion_scores": [
    {"criterion_id": "R1", "criterion": "DTC 확인", "score": 3, "max_score": 3, "answer_evidence": "DTC를 먼저 확인한다", "source_evidence_ids": ["EV-001"], "confidence": 0.95},
    {"criterion_id": "R2", "criterion": "CAN 상태 확인", "score": 1, "max_score": 2, "answer_evidence": "CAN 상태를 본다", "source_evidence_ids": ["EV-001"], "confidence": 0.80}
  ],
  "rubric_version": "1.0",
  "scale_max": 100,
  "llm_score": 80
}
```

#### 3.7.4. LLM D: 검증 / 설명

```text
LLM Score
Python Score
    ↓
차이 분석
    ↓
설명 생성
```

네 역할은 최대 4개의 논리적 역할이며 서로 다른 모델 4개를 동시에 메모리에 올려야 한다는 의미는 아니다. 초기에는 순차 실행과 역할별 모델 매핑을 사용하고 RAM/VRAM 사용량과 지연 시간을 측정한다. LLM D는 불일치 원인과 근거를 설명하며 확정 점수를 임의 변경하지 않는다.

## 3.8. LLM + Python Hybrid Scoring

```
인수자 답안
     ↓
LLM
     ↓
LLM Score

동시에

Rubric + 답안 분석
     ↓
Python
     ↓
Python Score
```

비교 방식:

- Python Score
- LLM Score
- Hybrid Score

초기 예시:

**HybridScore = 0.7 × PythonScore + 0.3 × LLMScore**

사람이 직접 채점한 `Human Score`를 Ground Truth로 두고 다음을 비교한다.

```text
Python vs Human
LLM vs Human
Hybrid vs Human
```

MAE와 판정 일치율을 이용해 최종 채점 방식을 선정한다.

#### 3.8.1. 점수 산출 경로와 한계

LLM은 사람 답안에 대한 예상 점수인 `llm_score`와 기준별 점수를 먼저 산출한다. Human Score는 LLM에게 공개하지 않는다. Python은 동일한 버전의 Rubric으로 별도 계산하고 LLM 총점을 입력으로 사용하지 않는다.

객관식, 정확 일치, 절차 순서 문제는 정답 비교 규칙으로 Python 기준 점수를 산출한다. 자유 서술형은 규칙 기반 매칭의 한계가 있으므로 기준 충족 여부를 사람이 라벨링한 실험 점수와 LLM이 추출한 충족 여부를 Python이 합산한 운영 점수를 구분한다. 후자는 의미 판단이 LLM에 의존하므로 완전히 독립적인 Python 평가로 보고하지 않는다. 각 결과에 `python_score_mode`를 기록한다.

기준별 점수는 Rubric의 허용 범위를 검증하고 `100 × 합계 / 만점 합계`로 정규화한다. 만점 합계가 0이거나 기준 누락 및 중복이 있으면 채점을 보류한다. LLM 총점과 기준별 점수 합산 결과가 허용 반올림 오차를 넘으면 오류로 처리한다. `confidence`는 검토용으로만 사용하며 점수에 곱하지 않는다. 한쪽 점수가 없으면 `Hybrid Score`도 생성하지 않고 사용 가능한 경로와 검토 상태를 표시한다.

#### 3.8.2. 채점 실험 프로토콜

1. 정답, 부분 정답, 오답, 무응답과 문항 유형별 사례를 구성하고 최소 2명의 사람이 독립 채점한다. 채점 차이를 기록한 뒤 합의한 점수를 Ground Truth로 확정한다.
2. 원본 업무/문서 단위로 개발, 검증, 최종 평가 데이터를 분리한다. 같은 문서의 스캔 품질 변형과 같은 답안의 표현 변형은 같은 분할에 둔다.
3. 개발 데이터로 Rubric을 정비하고 검증 데이터로 결합 가중치와 재검증 임계값을 선택한다. `0.7/0.3`과 `5점`은 초기 후보이며 최종 평가 데이터로 조정하지 않는다.
4. Python, LLM, Hybrid 각각의 MAE, 판정 일치율, 잘못된 PASS 비율, 검토 필요 비율을 문항 유형별로 비교한다. 인간 채점자 간 차이도 함께 보고한다.
5. 더 나은 검증 성능이 확인된 방식을 채택하고 가중치, 임계값, 데이터 분할, 모델, Rubric 버전을 고정한 뒤 최종 평가를 한 번 수행한다. 초기 소규모 결과는 탐색 결과로 표시한다.

#### 3.8.3. 점수 차이에 따른 검증 구조

```
LLM Score = 82
Python Score = 79
Difference = 3
```

`Difference = abs(LLM Score - Python Score)`로 정의한다. 두 점수가 유효하고 판정 Label도 같으며 근거 누락·충돌이 없는 경우에만 작은 차이를 자동 처리 후보로 삼는다.

```
Difference ≤ 5
+ 동일 Label + 유효한 점수/근거
→ 자동 처리 후보
```

차이가 크면 재검증 단계로 보낸다.

```
LLM Score ───┐
             ├→ Difference 검사
Python Score ─┘
                   │
             ┌─────┴─────┐
            작음          큼
             ↓            ↓
          자동 처리    재검증
```

초기 Label 기준은 80점 이상 `PASS`, 60점 이상 80점 미만 `RETRAIN`, 60점 미만 `INCOMPLETE`로 둔다. 반올림 전 점수로 판정한다. 79점과 81점처럼 차이가 작아도 Label이 다르면 검토한다. 큰 점수 차이, 낮은 근거 품질, 계산 모순은 LLM D의 설명을 첨부해 사람 검토로 보낸다. 재검증으로 해결되지 않은 건은 `needs_review`로 남기며 평균 점수만으로 통과시키지 않는다. 시스템의 판정은 참고 지표이며 실제 인수인계 완료 결정은 담당자가 한다.

### 3.9. Python Fallback Query

```
사용자 요청
     ↓
LLM
     ↓
Intent JSON
     ↓
Schema Validation
```

정상일 경우 Backend가 Intent에 대응하는 사전 정의 Query/분석 함수를 실행하고 실패하면 Python 기반 Fallback을 사용한다. LLM은 임의 SQL을 직접 실행하지 않는다.

```
Regex
Keyword Matching
Predefined Query Mapping
```

```
LLM 정상
 → Intent JSON → 허용된 Query Mapping

LLM 오류
 → Python Fallback Query
```

#### 3.9.1. 오류별 상태 전이

| 조건 | 처리 |
|---|---|
| timeout, 연결 실패, 모델 미가동 | 일시 오류는 요청당 최대 1회 재시도, 이후 Fallback |
| 빈 응답, JSON/Schema 오류, 미허용 Intent | 최대 1회 구조 보정 재시도 후 Fallback |
| Python 규칙으로 유일한 Intent 및 필수 인자 확인 | 매개변수화한 사전 정의 Query 실행, `degraded` 기록 |
| Fallback에서도 대상 및 요청이 모호함 | `needs_input` 반환, 필요한 정보를 요청 |
| DB 장애 또는 허용 Query 실행 실패 | `failed` 반환, 성공처럼 빈 결과를 만들지 않음 |
| 평가 근거 누락, 범위 밖 점수, 반복 불일치 | Query Fallback과 구분하여 `needs_review` 처리 |

전체 재시도 횟수는 오류 종류가 달라져도 요청당 1회로 제한한다. timeout 기본값은 역할별 설정으로 관리하며 초기 60초에서 로컬 측정 후 조정한다. Intent 종류, 대상 ID, 조회 한도와 접근 가능한 컬럼을 검증한다. 알려진 규칙으로 처리 가능한 요청만 Fallback에서 실행한다.

`request_id`, `status`, `fallback_used`, `fallback_reason`, `attempts`, `provider`, `latency_ms`, `query_mapping_version`을 실행 이력과 결과 메타데이터에 남긴다. UI에는 대체 처리 여부와 확인 필요 상태를 표시한다. Query Fallback이 문서 생성 및 서술형 채점까지 완전히 대체한다고 가정하지 않는다.

---

## 4. 인수인계서 데이터

### 4.1. 인수인계서 데이터 구축

실험용 Ground Truth를 직접 구축한다.

```
업무 100개
직원 5~10명

업무별
- 중요도
- 절차
- 빈도
- 실패 영향
- 필요 기술
- 예상 업무 시간
- 관련 문서
```

Clean Data를 기준으로 스캔 PDF를 제작한다.

Task에는 원본 PDF의 `dependency_score`, `urgency`, `required_skills` 수준을 포함한다. Employee에는 기술별 수준, 경험 기간, 가용 시간, 기존 수행 업무를 저장한다. Question에는 문항 ID/유형, 업무 ID, 정답, 난이도, Rubric, 만점, 근거 ID와 버전을 함께 보존한다. 생성 인수인계서는 Schema로 별도 관리한다.

이미지 노이즈와 업무 내용 오류를 구분한다. Clean 원본을 보존하고 결측값, 중복, 단위 불일치, 범위 밖 수치, 오래된 담당자, 문서 간 충돌을 삽입한 데이터에는 오류 위치와 유형의 정답 라벨을 붙인다. 오류 탐지율과 수정 결과를 원본과 비교한다.

---

### 4.2. 스캔 PDF 데이터셋 제작

#### 4.2.1. Good Scan

```
300 DPI
왜곡 거의 없음
```

#### 4.2.2. Medium Scan

```
200 DPI
약간 회전
JPEG Compression
```

#### 4.2.3. Poor Scan

```
150 DPI
Noise
Blur
Skew
```

#### 4.2.4. 평가 항목

```
OCR 정확도
구조 분석 정확도
RAG 검색 정확도
```

---

## 5. 최종 시스템 아키텍처

### 5.1. 최종 권장 시스템 아키텍처

```
Scanned PDF → PyMuPDF → PaddleOCR → Layout 분석
                                      │
                                      ▼
                         LLM A / 업무 정보 구조화
                                      │
                                      ▼
                    Schema·근거 검증 → SQLite Repository
                                      │
                             Chunk → Embedding
                                      │
                                      ▼
                               ChromaDB Index

문서 작성 요청 → SQL 필터 + RAG 검색 → Gateway.generate_report()
                                          │
                                출력 검증 → Jinja2 인수인계서

문제 생성 요청 → RAG 검색 → LLM B → 문제·정답·Rubric·근거 검증
                                          │
                                      인수자 답안
                                          │
                   ┌──────────────────────┴─────────────────┐
                   ▼                                        ▼
          LLM C / LLM Score                  Python Score Engine
                   │                         (평가 모드 구분)
                   └──────────────────────┬─────────────────┘
                                          ▼
                            점수 검증·차이·Label 비교
                                          │
                          ┌───────────────┴──────────────┐
                          ▼                              ▼
                   채택된 점수 정책              LLM D 설명 + 사람 검토
                          │                              │
                          └───────────────┬──────────────┘
                                          ▼
                           Python 업무 분석 / 준비도·추천
                                          │
                               검증된 Result JSON
                                          │
                                FastAPI → React + Vite
```

문서 생성은 기존 역할의 모델을 Gateway를 통해 재사용하며 별도의 다섯 번째 모델 역할을 필수로 두지 않는다. 모든 LLM 경로는 공통 Gateway, Schema, 실행 이력을 사용한다. 점수 정책은 Python/LLM/Hybrid 실험 후 결정하며 검토 미완료 점수로 확정 준비도 및 추천 결과를 만들지 않는다.

#### 5.1.1. 업무 분석 기능의 유지

원본 PDF의 업무 영향도, 적합도, 위험도, 준비도 분석은 Python 도메인 로직으로 유지한다. 다음은 초기 가설 수식이며 변수는 0~100으로 정규화하고 가중치는 버전 관리한다.

```
Impact = 0.32 × Criticality + 0.24 × FailureImpact
       + 0.20 × Dependency + 0.12 × Frequency + 0.12 × Urgency
SkillMatch = 100 × Σ[r_k × min(p_k / r_k, 1)] / Σ[r_k]
Fit = 0.55 × SkillMatch + 0.30 × Experience + 0.15 × Availability
AssignmentScore = Impact × Fit / 100
RiskGap = Impact × (1 - Fit / 100)
Readiness = 0.50 × QuizScore + 0.35 × CriticalTaskFit + 0.15 × TaskCoverage
```

`r_k`는 요구 기술 수준 `p_k`는 보유 수준이며 요구 수준 0은 계산에서 제외한다. Experience와 Availability는 업무별 기준 경험 기간 및 소요 시간을 이용해 0~100으로 정규화하며 상한을 적용한다. `CriticalTaskFit`은 Impact 80 이상 업무의 평균 `Fit`, `TaskCoverage`는 전체 `Impact` 합계 중 `Fit` 60 이상 업무의 Impact 합계 비율이다. 분모가 0이거나 기준값 및 필수 입력이 없으면 해당 지표를 `null`과 사유로 표시하고 준비도 확정을 보류한다.

추천 수 `N`은 설정값으로 MVP 5개 확장 20개를 사용한다. `AssignmentScore` 내림차순 및 task_id 오름차순으로 선택하고 `RiskGap` 상위 업무는 추가 교육 대상으로 별도 제시한다. 인수인계 완료 판단은 점수만으로 자동 확정하지 않는다.

## 5.2. 최종 기술 스택

| 영역 | 기술 | 선정 이유 |
|---|---|---|
| Frontend | React + Vite | 화면 구성이 쉽고 모델 및 DB와 독립적 |
| Backend | Python + FastAPI | AI/OCR/분석 라이브러리와 연결 용이 |
| PDF | PyMuPDF | PDF 페이지 렌더링 및 페이지 관리 용이 |
| OCR | PaddleOCR | 로컬 한국어 OCR 가능 |
| Layout | PaddleOCR Layout 계열 + Python 후처리 | 제목, 표, 영역 분석 |
| Structured DB | SQLite | 서버 없이 구현 가능, SQL 및 JSON 활용 가능 |
| Vector Store | ChromaDB | OCR Chunk와 Embedding 저장이 단순 |
| Embedding | Sentence Transformers | 모델 교체가 쉽고 로컬 동작 가능 |
| LLM Runtime | Ollama | 여러 LLM 교체 실험이 쉬움 |
| Model Interface | Custom LLM Gateway | 특정 LLM 종속성 제거 |
| Validation | Pydantic | 모든 LLM 출력 Schema 통일 |
| Scoring | Python + NumPy/Pandas | 재현 가능한 계산 |
| RAG | 직접 구현 | 구조 이해, 제어, 디버깅 용이 |
| Template | Jinja2 | LLM과 최종 문서 포맷 분리 |

### 5.3. 개발 단계

#### 5.3.1. 데이터 제작

- 업무 데이터 20~30개
- 인수자 3명
- 인수인계 PDF 3~5개
- Scan Quality 3단계

#### 5.3.2. OCR + 문서 구조화

- 페이지별 OCR 가능
- Section 계층 저장
- 출처 페이지 추적 가능

#### 5.3.3. RAG

- OCR Chunk Embedding
- Vector Store 저장
- Top-K 검색
- Source Page 표시

#### 5.3.4. 인수인계서 작성

```
RAG
↓
LLM
↓
Structured JSON
↓
Template
↓
인수인계서
```

#### 5.3.5. 문제 생성

```
RAG
↓
LLM B
↓
문제 / 정답 / Rubric / Evidence
```

#### 5.3.6. 이중 점수 계산

```
인수자 답변
↓
LLM Score
+
Python Score
↓
비교
↓
Hybrid Score
```

#### 5.3.7. UI

- 문서 등록
- 문서 구조 확인
- 문제 풀이
- 점수 확인
- 업무 추천
- 인수인계서 확인

#### 5.3.8. 교체, 장애, 전체 흐름 검증

| 검증 범위 | 완료 조건 |
|---|---|
| 데이터/OCR | Clean 원본과 품질 및 내용 오류 라벨 연결, 페이지, block, Chunk 출처 추적 |
| 저장/검색 | 갱신, 색인 실패, 재시도 후 중복 및 오래된 근거 미노출 확인 |
| 문서 작성 | Schema와 모든 근거 참조 검증, 누락 및 충돌 상태 표시 |
| 채점 | 고정 평가 데이터에서 세 채점 경로 비교, 가중치 및 판정 정책 버전 확정 |
| 모델 교체 | 기준 측정, 미충족 항목과 처리 정책 기록 |
| Fallback | timeout, 잘못된 JSON, 모호한 요청, DB 장애를 주입하여 상태 확인 |
| 업무 분석 | 수식, 경계값, 분모 0, 동점, 추천 개수에 대한 예상 결과 일치 |
| 전체 시연 | 스캔 PDF → 생성 문서 → 문제 → 답안 → 점수 → 준비도 및 추천 및 출처 확인 |

각 Phase 산출물에 입력 데이터 버전과 검증 결과를 남긴다. 작은 데이터셋으로 기능 연결을 검증한 뒤 규모를 확대한다. 모델 4개 동시 실행은 초기 완료 조건에 포함하지 않는다.

---

### 5.4. 평가 항목

#### 5.4.1. OCR

- Character Accuracy
- 중요 필드 Accuracy
- Heading 인식률

#### 5.4.2. RAG

- Top-1 Recall
- Top-3 Recall
- Top-5 Recall

#### 5.4.3. 문제 생성

- 근거 존재율
- 중복률
- 난이도 적절성
- Rubric 적절성

#### 5.4.4. 채점

- Human vs Python MAE
- Human vs LLM MAE
- Human vs Hybrid MAE
- PASS 판정 일치율

#### 5.4.5. 모델 독립성

- JSON Schema 성공률
- 필수 필드 일치율
- 결과 Label 일치율
- 점수 차이

#### 5.4.6. 운영 및 회복 가능성

- 최초 응답과 재시도 후 Schema 성공률, Fallback 발생률 및 성공률
- 검토 필요, 입력 필요, 실패 비율과 실패 원인
- 전체 및 단계별 처리 시간, RAM/VRAM 최대 사용량
- DB/Vector Store 동기화 지연, 색인 복구 성공률
- 생성 문서의 근거 참조 유효율과 내용의 근거 부합률(사람 검토)

평가에는 사용한 문서/업무/답안 수, 데이터 분할, 모델 및 설정 버전을 함께 보고한다. 성능 목표는 실제 로컬 장비에서 측정한 기준으로 확정한다.

### 5.5. 최종 개발 원칙

1. **1차 입력은 스캔 PDF로 제한하고 OCR 및 문서 구조 분석을 핵심 기술로 개발한다.**
2. **특정 LLM에 종속되지 않도록 Model Gateway와 공통 JSON Schema를 사용한다.**
3. **LLM의 자연어 출력 자체를 결과로 사용하기보다 구조화된 데이터를 프로그램의 표준 출력으로 사용한다.**
4. **문서 작성은 RAG 구조를 사용하고 모든 생성 정보에 원문 근거를 연결한다.**
5. **LLM과 Python이 각각 점수를 계산하고 사람의 정답과 비교하여 최종 채점 방식을 결정한다.**
6. **정형 데이터는 SQLite, 의미 검색 데이터는 Vector Store로 관리하는 Hybrid DB 구조를 사용한다.**
7. **PDF 목차·제목·페이지·본문의 계층 관계를 DB에 구조적으로 저장한다.**
8. **LLM 오류 발생 시 Python 기반 Fallback Query를 제공해 시스템이 특정 모델 오류에 의해 중단되지 않도록 한다.**
9. **필요에 따라 최대 4개의 LLM 역할을 분리하되 각 역할은 독립적으로 모델을 교체할 수 있게 한다.**
10. **프론트엔드, DB, LLM은 교체 가능한 계층으로 분리하고 핵심 분석 로직은 Backend에 유지한다.**

### 5.6. 최종 요약

```
스캔 PDF
→ OCR
→ 문서 구조화
→ Hybrid DB
→ RAG
→ 모델 독립적 LLM 처리
→ LLM/Python 이중 평가
→ 인수인계서 및 분석 결과 출력
```

구현 단계에서는 먼저 작은 데이터셋과 스캔 PDF로 OCR, 문서 구조화, RAG, 문제 생성, 이중 채점의 각 기능을 검증한 뒤 전체 시스템으로 통합한다.
