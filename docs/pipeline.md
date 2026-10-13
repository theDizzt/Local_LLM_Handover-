# 문서 처리 파이프라인: 등록 → OCR → 구조 → 배치 분석 → 검토

PDF가 들어와서 검색 가능한 근거 묶음이 되기 전까지의 단계별 구현 안내다.
검색·초안 생성은 [검색과 초안 생성](search-and-draft.md), 품질 측정은 [평가](evaluation.md)를 참고한다.

| 단계 | 화면 동작 | 결과 | 버전 키 |
|---|---|---|---|
| 1. 등록 | **PDF 등록** | 원본 파일 + 문서 메타데이터 | `document_id` |
| 2. OCR | **OCR 시작** | 페이지 PNG + OCR Block(텍스트·좌표·신뢰도) | `ingestion_id` |
| 3. 구조 | **근거 준비** (페이지 순서) | Section·Chunk·원문 연결 | `page-lines-v1:chars=800` |
| 4. 배치 분석 | **배치 분석 (시험)** → **근거 준비** | 영역·읽기 순서·검토 사유 | `geometry-layout-v1:chars=800` |
| 5. 검토 기록 | **검토 완료 / 수정 필요 저장** | 페이지별 검토 revision 이력 | `(structure_id, page_id, revision)` |

모든 단계는 단일 서버 프로세스의 `BackgroundTasks`를 전제로 한다. `--workers`를 늘리지 않는다.
시작 시 남은 queued/running 작업은 중단 상태로 바꾸므로, 여러 서버가 같은 DB를 공유하려면
별도 작업 큐와 worker 소유권/lease가 먼저 필요하다. `.env.example`은 참고용이며 앱은 `.env`를
자동으로 읽지 않는다.

---

## 1. 문서 등록

### 요청 처리 흐름

`API → DocumentService → PdfInspector / DocumentRepository`

1. API가 multipart/form-data의 `file`을 받는다.
2. 서비스가 표시용 파일명을 정리하고 임시 파일에 1 MiB씩 복사하면서 실제 크기와 SHA-256을 계산한다.
3. PyMuPDF 어댑터가 실제 PDF인지, 암호가 필요한지, 페이지가 있는지 검사한다.
4. 같은 해시가 이미 등록되어 있으면 기존 문서를 반환하고 임시 파일을 삭제한다.
5. 새 문서이면 UUID 기반 파일명으로 원본을 저장하고 ingestion/document를 하나의 SQLite 트랜잭션으로 저장한다.
6. 신규는 201, 중복은 200을 반환한다. 두 응답 모두 Location 헤더로 상세 API 경로를 제공한다.

API 사용 예와 응답 코드는 루트 [README](../README.md#문서-등록-api)에 있다.

### 파일별 역할

| 파일 | 역할 |
|---|---|
| `domain/documents.py` | 공개 응답 모델, 내부 저장 모델, 등록 오류 종류 |
| `ports/repositories.py` | 저장소 교체를 위한 타입이 명시된 문서 계약 |
| `ports/pdf_inspector.py` | PDF 검사 라이브러리 교체 계약 |
| `adapters/pdf.py` | PyMuPDF로 파일 확인 및 페이지 수 조회 |
| `adapters/document_repository.py` | 매개변수화 SQL, 등록 트랜잭션, 목록·상세 조회 |
| `services/documents.py` | 파일 복사, 해시, 중복 판별, 저장 및 실패 시 정리 |
| `api/routes/documents.py` | HTTP 입력·출력과 오류 상태 코드 |
| `api/dependencies.py` | 서비스에 실제 어댑터를 주입하는 조립 지점 |

### 저장 규칙

- 원본은 로컬 파일 저장소에, 메타데이터는 SQLite에 보관한다. 원본 경로는 내부 `StoredDocument`에만 있고 API 응답에서 제외한다.
- 신규 등록은 `document_version=1`, `status=pending`, OCR/parser 버전 `not-run`으로 시작한다. 업로드 성공이 OCR 완료를 뜻하지 않는다.
- 동일 이름의 다른 PDF는 별도 문서다. 동일 내용은 이름이 달라도 중복이다.
- DB 스키마는 UNIQUE(file_sha256, document_version)를 사용한다. 재처리/개정은 OCR 단계의 처리 버전으로 다룬다.

### 실패와 동시 요청

ingestion과 document 삽입은 같은 트랜잭션이다. 동시 업로드가 모두 사전 중복 조회를 통과해도
DB UNIQUE 제약이 한 요청만 저장하게 하고, 나머지는 기존 결과를 반환한다.

원본 파일은 DB 저장 전에 최종 경로로 이동하고, DB 저장 실패 시 이번 요청의 원본과 임시 파일을
정리한다. 다만 파일시스템과 SQLite는 하나의 트랜잭션이 아니므로 강제 종료/정전 시 DB가 참조하지
않는 파일이 남을 수 있다. 자동 고아 파일 회수는 아직 없다.

파일 크기 제한은 multipart 파싱 후 서비스에서 적용하는 원본 등록 제한이며 HTTP 요청 전체의
수신량 제한은 아니다. 배포 시 요청 본문 제한은 별도로 설정해야 한다.

테스트는 임시 디렉터리와 실제 PDF로 등록·재시작·중복·목록, 손상/빈/암호화 파일 거부,
크기 제한, DB 및 파일 이동 실패, 동시 중복 등록을 검증한다.

---

## 2. PDF 렌더링과 OCR

### 실행

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev,ocr]"
.\run.bat
```

<http://127.0.0.1:8000/>에서 화면을, `/docs`에서 API를 사용한다. FastAPI가 화면을 함께 제공하므로
Node 개발 서버나 인터넷 폰트가 필요 없다.

| API | 동작 |
|---|---|
| `POST /api/v1/documents/{document_id}/ocr` | 새 처리 버전을 만들고 202/작업 ID 반환 |
| `GET /api/v1/ocr-jobs/{job_id}` | 상태·완료 페이지 수·실패 코드 조회 |
| `GET /api/v1/documents/{document_id}/ocr/latest` | 최신 작업 조회; 실행 전에는 null, 없는 문서는 404 |
| `GET /api/v1/documents/{document_id}/pages` | 마지막 성공 버전의 페이지와 OCR Block 조회 |
| `GET /api/v1/documents/{document_id}/pages/{page_id}/image` | 원문 PNG 조회 |

동일 문서의 동시 실행은 409, OCR 패키지 미설치는 503이다. 모델 초기화/다운로드 실패는
`failed / ocr_unavailable`, 그 외 실패는 `processing_failed`, 서버 재시작으로 중단된 작업은
`interrupted`로 남는다. 같은 POST를 다시 호출하면 새 ingestion_id로 재시도한다.

### 화면 사용

1. PDF를 선택하고 **PDF 등록**을 누른다. 동일 내용이면 기존 문서가 선택된다.
2. **OCR 시작**을 누른다. 저장된 페이지 수를 표시하며 처리 중에는 중복 실행을 막는다.
3. 완료 후 페이지별 이미지와 텍스트를 비교한다. 문장을 선택하면 원문 영역이 강조된다.
4. 작은 글씨는 **원본 크기로 보기**로 확인한다. **신뢰도 80% 미만만**은 검토 대상을 좁히는
   임시 기준이며 정확도/합격 기준이 아니다.
5. 실패 원인을 확인하고 **OCR 다시 실행**을 누른다. 이전 성공 결과는 보존된다.

선택 문서는 URL에 남고, 새로고침하면 최신 작업을 조회해 진행 상태를 복원한다. 문서의 `ready`와
최신 작업의 `failed`가 함께 있을 수 있으며, 이때 화면은 실패 이유와 이전 성공 결과를 함께 보여 준다.
목록은 10개, 결과는 한 페이지씩 조회한다. OCR 텍스트를 직접 수정하는 편집기는 아직 없다.

### 원문과 좌표

PyMuPDF가 PDF 회전 메타데이터를 반영해 PNG를 만든다. OCR 자동 문서 회전/왜곡 보정은 꺼서 좌표가
PNG와 일치하게 한다. bbox는 `[x0,y0,x1,y1]`, 각 값 0~1이며 표시 이미지의 너비/높이를 곱하면 화면 좌표다.

OCR 텍스트 행을 순서대로 저장하며 `block_type=paragraph`는 임시 분류, 구조 신뢰도는 0이다.
OCR 원문에는 Layout 분류를 덮어쓰지 않는다. 텍스트가 없는 페이지도 빈 blocks와 함께 저장한다.
OCR 완료는 검색 색인 완료나 내용 정확성 보장을 뜻하지 않는다.

### 버전과 실패 처리

새 OCR 작업은 항상 별도 ingestion과 이미지 폴더를 사용한다. 전체 페이지 저장이 끝난 경우에만
document의 ingestion_id를 바꾼다. 이전 Page/Block은 남고 `pages?ingestion_id=...`로 과거 성공 버전을
조회할 수 있다. 이전 Chunk는 새 버전 검색에 섞이지 않도록 `index_status=failed`로 바꾼다.

실패 작업의 부분 DB/이미지는 진단용으로 남지만 공개 API는 ready 버전만 제공한다.
실패 파일 자동 정리, 취소, 자동 재시도는 후속 작업이다.

### 설정

| 변수 | 의미 |
|---|---|
| `PAGE_STORE_PATH` | PNG 저장 위치, 기본 `data/pages` |
| `OCR_DPI` | 72~300, 기본 150 |
| `OCR_MAX_PIXELS` | 페이지당 기본 25,000,000 |
| `OCR_LANGUAGE` | `korean`(기본) 또는 `en` |
| `OCR_DETECTION_MODEL_DIR`, `OCR_RECOGNITION_MODEL_DIR` | 준비된 로컬 모델 폴더. 생략하면 첫 실행에서 공개 모델을 다운로드할 수 있다 |
| `PADDLE_PDX_CACHE_HOME` | PaddleX 캐시 경로 |

CPU용 PP-OCRv5 mobile 검출 모델과 언어별 mobile 인식 모델을 명시해 사용하며 CPU 스레드는 2개로
제한한다. Windows CPU에서 확인된 oneDNN/PIR 연산 오류를 피하려고 `enable_mkldnn=False`다.
OCR 3.x의 `predict`와 결과 필드는
[PaddleOCR 공식 문서](https://github.com/PaddlePaddle/PaddleOCR/blob/main/docs/version3.x/pipeline_usage/OCR.en.md)를 기준으로 연결했다.

### 코드 위치 (화면)

- `main.py`: `/` 화면과 `/static` 자산 제공. data 폴더는 공개하지 않는다.
- `web/index.html`: 업로드, 문서 목록, 진행 상태, 원문/텍스트 비교 영역.
- `web/review.js`: API 호출·폴링·페이지 이동·강조 좌표·상태 복원. 요청 세대 번호로 다른 문서의
  늦은 응답을 무시하며 파일명과 OCR 내용은 textContent로 넣는다.
- `web/review.css`: 녹색 기반 화면과 모바일 세로 배치.
- `SQLiteOcrRepository.latest_job`: 생성 시각이 같으면 rowid로 최신 작업을 결정한다.

### 검증

- 기본 `python -m pytest -q`는 모델 다운로드 없이 실제 PDF 렌더링/DB/API와 fixture OCR을 검증한다.
- 실제 엔진 선택 테스트: `$env:RUN_OCR_LIVE="1"` 후 `python -m pytest tests/test_ocr_live.py -q -s`.
  Good/Medium/Poor 합성 스캔에서 결과 형식과 신뢰도 범위를 확인한다.
- 2026-09-30: PaddleOCR 3.7.0 / PaddlePaddle 3.3.1 / Windows CPU에서 통과. 합성 영문 문장 인식이며
  한글 인식률 검증은 아니다.
- 한국어 문자 오류율 측정은 [평가 > OCR 문자 정확도](evaluation.md#1-ocr-문자-정확도)를 참고한다.

---

## 3. 문서 구조·Chunk·원문 근거 (페이지 순서 방식)

### 사용 방법

OCR 완료 후 **근거 준비**를 누르면 페이지별 근거 묶음을 저장한다. 하단 **이 페이지의 근거 묶음**에서
항목을 선택하면 해당 OCR 행의 원문 영역을 모두 강조한다. 근거가 많으면 **근거 더 보기**로 추가 조회한다.

같은 버전에서 다시 눌러도 중복 생성하지 않는다. OCR 재처리에 성공하면 새 버전의 근거를 다시
준비한다. 다른 탭에서 OCR 버전이 바뀌면 새로고침 안내를 표시한다.

### 규칙과 한계 (`page-lines-v1:chars=800`)

- 페이지마다 임시 Section을 만든다. 제목 `1페이지`는 목차 추출 결과가 아니다(`title_source=inferred`, `structure_confidence=0`).
- OCR `reading_order`를 보존하고 행 사이 줄바꿈을 포함해 약 800자 단위로 묶는다. 800자는 토큰 수가 아니다.
- 페이지를 넘겨 묶지 않으며 원문 행을 중간에서 자르지 않는다. 800자를 넘는 단일 행은 하나의 긴 Chunk가 된다.
- 텍스트를 새로 쓰거나 요약하지 않는다. 공백뿐인 행은 제외하고, 빈 페이지는 Section만 만든다.
- Chunk OCR 신뢰도는 포함된 행 중 최솟값이다. 평균이 저품질 행을 가리지 않게 한다.
- 순서는 OCR 엔진이 저장한 순서이므로 복잡한 문서의 의미상 읽기 순서를 보장하지 않는다.

### 저장 구조와 버전 보존

```text
structure_run (문서 ID + ingestion_id + parser_version별 1개)
  └─ structure_section (Section ↔ 실행 버전 ↔ 원문 Page)
       ├─ section (페이지 기본 구조)
       └─ chunk
            └─ chunk_source → document_block → page → 원문 이미지
```

- 기존 `section`에 버전 열이 없어 별도 매핑 테이블로 버전을 명시한다. 기존 테이블/열은 바꾸지 않고
  `CREATE TABLE IF NOT EXISTS`로 추가한다. 새 구조 규칙은 `structure_run.parser_version`으로 관리한다.
- `document_block.section_id`는 덮어쓰지 않는다. 다른 Layout 규칙으로 재분석할 때 과거 해석이
  손상되지 않게 하기 위해서다.
- `BEGIN IMMEDIATE` 후 현재 OCR 버전과 완료 상태를 다시 확인하고, 구조 기록·Section·Chunk·원문 연결을
  하나의 트랜잭션으로 저장한다. 중간 실패는 모두 롤백하고 반복/동시 요청은 같은 결과를 반환한다.
- ID는 문서/처리/규칙/페이지/순서에서 결정적으로 생성한다. OCR 재처리 후 예전 Chunk는 `failed`가
  되지만 원문 근거 조회는 계속 허용한다(검색 가능하다는 뜻은 아니다).
- 생성 문서는 `evidence_id`, `chunk_id`, `ingestion_id`, `block_ids`를 고정 저장해야 한다.

### API

| 요청 | 의미 |
|---|---|
| `POST /api/v1/documents/{id}/structure` | 현재 OCR 버전의 구조/Chunk 생성 또는 기존 결과 반환 |
| `GET /api/v1/documents/{id}/structure` | 현재 OCR 버전의 구조 요약; 미생성이면 null |
| `GET /api/v1/documents/{id}/chunks` | 현재 OCR 버전의 Chunk 목록 (`page_number`, `limit` 1~100, `offset`) |
| `GET /api/v1/documents/{id}/chunks/{chunk_id}` | 특정 Chunk와 원문 근거; 과거 성공 버전도 허용 |

POST 본문은 `{"ingestion_id": "ING-..."}`이며 화면에서 확인한 버전을 필수로 보낸다.
OCR 미완료·버전 변경은 409, 없는 문서는 404, 잘못된 입력은 422다. GET은 `ingestion_id`로 과거 버전을
지정할 수 있다. Chunk 응답에는 `evidence`, `sources`(원문 텍스트·신뢰도·bbox), `page_id`,
`section_title`, `text_hash`(UTF-8 SHA-256)가 포함된다.

### 코드 위치

`domain/chunks.py`(분할 규칙·ID·응답 계약), `ports/chunks.py`, `services/chunks.py`(버전 고정·분할·저장 조합),
`adapters/chunk_repository.py`(트랜잭션·버전 검증·근거 조립), `api/routes/chunks.py`, `web/review.js`.

---

## 4. 규칙 기반 배치 분석 (시험)

### 사용 방법

1. OCR 완료 후 **정리 방식**에서 **배치 분석 (시험)**을 선택하고 **근거 준비**를 누른다.
2. **배치 분석 · 검토 필요**에서 영역과 사유를 확인한다. 영역 버튼은 원문 좌표를 강조한다.
3. **페이지 순서**로 전환하면 기존 결과와 비교할 수 있다. 전환은 결과를 삭제하지 않는다.

선택 방식은 URL에 남아 새로고침 후 복원된다. 각 방식은 해당 OCR 버전에 고정된다.

### 규칙

`GeometryLayoutAnalyzer`는 저장된 OCR 텍스트·사각형만 읽는 규칙 기반 분석기로, 모델 다운로드 없이
동작한다. 모델 기반 분석기가 필요하면 `LayoutAnalyzer` 포트를 구현해 교체한다.

| 대상 | 제안 규칙 | 표시와 한계 |
|---|---|---|
| 제목 | 독립된 짧은 행(80자 이하), 높이가 중앙값의 1.5배 이상 | 제목 후보; 번호만으로 판정하지 않음 |
| 본문 | 단일 열의 위→아래 순서 | 텍스트 그대로 보존 |
| 2단 | 페이지 폭 4% 이상 수직 여백, 양쪽 최소 2행, 세로 범위 겹침 | 왼쪽→오른쪽 열 제안; 짧은 텍스트만 있으면 보류 |
| 표 | 짧은 텍스트 3열 이상이 3행 이상 정렬 | 표 후보; 행 우선 순서, 셀/병합은 복원하지 않음 |
| 짧은 2열 | 표/목록/다단 구분 정보 부족 | 판별 보류, OCR 순서 유지 |
| 겹침/복잡 | 작은 사각형 면적 30% 초과 겹침 또는 1,500행 초과 | 판별 보류, OCR 순서 유지 |
| 빈 페이지 | 유효한 OCR 텍스트 없음 | 빈 페이지/인식 누락 검토 안내 |

표 후보는 열 시작 좌표 차이 0.025 이내, 셀 텍스트 24자 이하, 행간 간격은 대표 글자 높이의 3배
이내라는 초기 규칙이다. 실제 문서로 정한 기준이 아니므로 조정 시 `parser_version`을 올린다.
전체 폭 제목/본문은 앞뒤 구간을 나눠 분석하고, 혼합 배치나 3단 본문은 원래 순서를 유지한다.
모든 결과는 `heuristic_layout` 검토 사유를 갖는다. 낮은 OCR 신뢰도는 별도 사유다.

페이지당 Section 하나를 유지하고 첫 영역이 제목 후보이면 Section 제목에 표시한다. 계층형 목차,
표 구조 복원, 그림/캡션/각주 인식, 회전된 글줄 교정은 후속 범위다.

### 버전과 원문 보존

- 같은 OCR 버전에서 `page-lines-v1`과 `geometry-layout-v1` 구조/Chunk가 서로 다른 ID로 병존한다.
- 영역 경계를 넘어 제목/본문/표 후보를 한 Chunk로 합치지 않는다. 영역 안에서는 800자 분할을 재사용한다.
- 모든 공백 아닌 Block이 정확히 한 영역에 한 번 나타나는지, 영역 bbox가 소속 Block 범위와 같은지
  검사한다. 누락/중복/다른 페이지/잘못된 좌표를 반환하는 분석기는 저장 전에 거절한다.
- DB 원문의 `reading_order`, 텍스트, bbox, 신뢰도, `section_id`는 변경하지 않는다.
- `layout_page` 테이블에 구조 ID별 영역·순서·검토 사유를 저장한다. Layout·Section·Chunk·원문 연결은
  한 트랜잭션이며 실패해도 기본 방식은 손상되지 않는다.

### API

| 경로 | 동작 |
|---|---|
| `POST /api/v1/documents/{id}/layout` | 현재 OCR의 배치 분석 저장; 같은 요청은 기존 결과 반환 |
| `GET /api/v1/documents/{id}/layout` | 분석 요약(`layout_mode=geometry_v1`, `review_page_count`) |
| `GET /api/v1/documents/{id}/layout/pages` | 페이지별 영역·읽기 순서·검토 사유 |
| `GET /api/v1/documents/{id}/layout/chunks` | 배치 분석 버전의 근거 묶음 |

POST 본문은 `{"ingestion_id":"ING-..."}`다. pages/chunks는 `limit`(1~100), `offset`을 지원하고
chunks는 `page_number`도 지원한다. 개별 Chunk는 `/documents/{id}/chunks/{chunk_id}`로 두 방식 모두
조회하며 `content_type`으로 구분한다. 페이지 응답의 `regions` 배열과 각 `block_ids` 순서가 제안 읽기 순서다.

### 검증

- `tests/fixtures/layout_cases.json`: 한국어 제목/본문, 2단, 3열 표, 모호한 짧은 2열, 겹침, 빈 페이지의
  정답 순서·분류 Golden fixture 6종(사람이 구성한 좌표).
- API/DB 테스트: 버전 공존, 원문 불변, 과거 근거, 저장 실패 복구, 잘못된 분석 결과 차단, 기존 DB 호환성.
- 저장된 OCR을 재분석하는 명령(앱 DB는 변경하지 않음):

```powershell
.\.venv\Scripts\python.exe scripts/validate_layout.py --ocr-report data/ocr-review-korean-verified/report.json --output data/layout-review-new
```

입력 보고서 폴더에 `validate_ocr.py`가 저장한 `pages/page-00001.png` 등이 있어야 한다.
결과의 분류 수는 정확도 점수가 아니다. 정답 대비 측정은 [평가 > 배치 분석 정답 평가](evaluation.md#2-배치-분석-정답-평가)를 쓴다.

---

## 5. 페이지별 검토 기록

자동 분석의 제안과 사람이 원문을 대조한 기록을 구분한다. 검토 저장은 OCR 텍스트·좌표·읽기 순서·
Chunk·자동 경고를 바꾸지 않는다.

### 화면 사용

1. **배치 분석 (시험)**으로 근거를 준비한다.
2. 페이지 이미지·텍스트·영역 버튼으로 숫자·누락·읽기 순서·분류를 확인한다.
3. 세 항목을 모두 확인하면 **검토 완료 저장**, 문제가 있으면 메모를 쓰고 **수정 필요 저장**을 누른다.
4. 다시 검토할 때는 **미검토로 되돌리기**를 누른다. 이전 기록은 이력에 남는다.

상태·메모·확인 항목·저장 번호·시간(UTC)이 SQLite에 보존되고 문서 전체의 완료/수정 필요/미검토 수를
표시한다. 메모는 2000자 이내이며 수정 필요에는 메모가 필수다. 화면은 최근 10개 이력을 보여 준다.

### 버전·동시 편집

- 키는 `(structure_id, page_id, revision)`이다. 페이지 순서 기본 방식에는 적용하지 않는다.
- OCR 재처리 후 새 분석은 미검토로 시작한다. 과거 기록은 조회만 가능하다.
- 저장 요청에는 `expected_revision`이 필요하다. 두 탭에서 동시에 수정하면 하나만 성공하고 나머지는 409다.
  화면은 실패한 입력을 보존한다. **저장 상태 다시 불러오기**는 입력을 서버 값으로 바꾸므로 메모를 먼저 보관한다.
- 최신 버전 검증, revision 비교, 이력 추가를 한 SQLite 쓰기 트랜잭션에서 실행한다.

검색 응답의 `human_review`에 최신 검토 상태가 들어간다(재색인 불필요). 자동 경고 `review_required`/
`review_reasons`는 사람이 확인해도 유지된다. 초안 생성은 기본적으로 검토 완료 근거만 사용한다.

### API

공통 경로: `/api/v1/documents/{document_id}/layout/{structure_id}`

| 메서드·경로 | 용도 |
|---|---|
| GET `/reviews` | 문서 전체 페이지 검토 집계 |
| GET `/pages/{page_id}/review` | 해당 버전 페이지의 최신 검토 기록 |
| PUT `/pages/{page_id}/review` | revision을 비교하여 새 기록 추가 |
| GET `/pages/{page_id}/review/history?limit=20&offset=0` | 최신순 이력 |

```json
{
  "expected_revision": 0,
  "status": "confirmed",
  "note": "원문 텍스트와 배치 대조 완료",
  "text_checked": true,
  "order_checked": true,
  "regions_checked": true
}
```

없는/다른 문서·구조의 페이지는 404, 오래된 OCR/편집 충돌은 409, 확인 항목·메모 누락·범위 오류는 422다.
현재 기록은 단일 로컬 작업자의 결과이며 사용자 인증/검토자 신원 증명은 없다. 검토 완료는 내용의
정확성 보증이 아니다.

---

## 브라우저 회귀 테스트

Playwright와 설치된 Edge를 사용한다. 빈 임시 DB에서 테스트하도록 서버를 새로 시작한다.
테스트 서버의 제어 API는 운영 앱에 포함되지 않으며 OCR 엔진은 fixture로 대체한다.

```powershell
.\.venv\Scripts\python.exe -m uvicorn review_server:app --app-dir tests --port 8765
# 별도 터미널. 합성 검증 도구가 만든 PDF 경로를 지정한다.
$env:REVIEW_PDF="data/ocr-review-korean-verified/synthetic-korean.pdf"
node tests/review.browser.mjs
```

Playwright 모듈이 다른 위치에 있으면 `PLAYWRIGHT_MODULE`, 브라우저 변경은 `BROWSER_CHANNEL=chrome`을 쓴다.
