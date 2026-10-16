# 문서 처리 파이프라인

문서 등록 → OCR → 검토 화면 → 구조·Chunk → 배치 분석 → 검토 기록.

## 목차

1. [문서 등록 구현](#문서-등록-구현)
2. [PDF 렌더링 및 OCR](#pdf-렌더링-및-ocr)
3. [문서 입력 및 OCR 검토](#문서-입력-및-ocr-검토)
4. [문서 구조·Chunk·원문 근거 연결](#문서-구조chunk원문-근거-연결)
5. [배치 분석과 검토](#배치-분석과-검토)
6. [페이지별 배치 분석 검토 기록](#페이지별-배치-분석-검토-기록)

---

## 문서 등록 구현

### 요청 처리 흐름

`API → DocumentService → PdfInspector / DocumentRepository`

1. API가 multipart/form-data의 `file`을 받는다.
2. 서비스가 표시용 파일명을 정리하고 임시 파일에 1 MiB씩 복사한다. 이때 실제 파일 크기와 SHA-256 해시를 계산한다.
3. PyMuPDF 어댑터가 실제 PDF인지, 암호가 필요한지, 페이지가 있는지 검사한다.
4. 같은 해시가 이미 등록되어 있으면 기존 문서를 반환하고 임시 파일을 삭제한다.
5. 새 문서이면 UUID 기반 파일명으로 원본을 저장하고 ingestion/document를 하나의 SQLite 트랜잭션으로 저장한다.
6. 신규 문서는 201, 중복 문서는 200으로 반환한다. 두 응답 모두 Location 헤더로 문서 상세 API 경로를 제공한다.

### 파일별 역할과 주석

| 파일 | 역할 |
|---|---|
| `domain/documents.py` | 공개 응답 모델, 내부 저장 모델, 등록 오류 종류 |
| `ports/repositories.py` | 저장소 교체를 위한 타입이 명시된 문서 계약 |
| `ports/pdf_inspector.py` | PDF 검사 라이브러리 교체 계약 |
| `adapters/pdf.py` | PyMuPDF로 파일 확인 및 페이지 수 조회 |
| `adapters/document_repository.py` | 매개변수화 SQL, 등록 트랜잭션, 목록 · 상세 조회 |
| `services/documents.py` | 파일 복사, 해시, 중복 판별, 저장 및 실패 시 정리 |
| `api/routes/documents.py` | HTTP 입력 · 출력과 오류 상태 코드 |
| `api/dependencies.py` | 서비스에 실제 어댑터를 주입하는 조립 지점 |

코드의 주석은 특히 파일 경로를 UUID로 만드는 이유, 동기 라우트를 사용하는 이유, 동시 중복 요청에서 UNIQUE 제약이 필요한 이유, 실패 시 정리 순서를 설명한다.

### 저장 규칙

- 원본은 로컬 파일 저장소에 메타데이터는 SQLite에 보관한다.
- 원본 경로는 내부 `StoredDocument`에만 포함되며 API 응답에서 제외한다.
- 신규 등록은 `document_version=1`, `status=pending`, OCR/parser 버전은 `not-run`으로 시작한다. 업로드 성공이 OCR 완료를 의미하지 않는다.
- 동일 이름의 다른 PDF는 별도 문서로 등록한다. 동일 내용은 다른 이름이어도 중복이다.
- 재처리/개정 기능은 이번 API에 포함되지 않는다. 향후 별도 동작에서 처리 버전을 만든다.
- DB 스키마는 기존 테이블과 UNIQUE(file_sha256, document_version)를 사용한다.

### 실패와 동시 요청

ingestion 및 document 삽입은 같은 트랜잭션으로 처리한다. 후자가 실패하면 전자도 롤백한다. 동시 업로드가 모두 사전 중복 조회를 통과하더라도 DB UNIQUE 제약이 한 요청만 저장하게 하며, 나머지는 기존 결과를 반환한다.

원본 파일은 DB 저장 전에 최종 경로로 이동한다. DB 저장 실패 시 이번 요청의 원본과 임시 파일을 정리한다. 따라서 정상 예외 처리에서는 DB가 없는 파일을 참조하지 않는다. 다만 파일시스템과 SQLite는 하나의 트랜잭션이 아니므로 프로세스 강제 종료/정전 시 DB에서 참조하지 않는 파일이 남을 수 있다. 자동 고아 파일 회수는 아직 구현하지 않았다.

파일 크기 제한은 multipart 파싱 후 서비스에서 적용된다. 이는 원본 등록 크기 제한이며 HTTP 요청 전체의 수신량 제한은 아니다. 배포 시 요청 본문 제한은 별도로 설정해야 한다.

### 검증

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check src tests
```

테스트는 임시 디렉터리와 실제 PDF를 사용한다. 등록 · 재시작 · 중복 · 목록 페이지 처리, 손상/빈/암호화 파일 거부, 크기 제한, DB 및 파일 이동 실패, 동시 중복 등록을 검증한다.

---

## PDF 렌더링 및 OCR

### 실행

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev,ocr]"
.\run.bat
```

`/docs`에서 PDF를 등록하고 반환된 문서 ID로 아래 API를 호출합니다.

| API | 동작 |
|---|---|
| `POST /api/v1/documents/{document_id}/ocr` | 새 처리 버전을 만들고 202/작업 ID 반환 |
| `GET /api/v1/ocr-jobs/{job_id}` | 상태·완료 페이지 수·실패 코드 조회 |
| `GET /api/v1/documents/{document_id}/ocr/latest` | 최신 작업 조회; 실행 전에는 null, 없는 문서는 404 |
| `GET /api/v1/documents/{document_id}/pages` | 마지막 성공 버전의 페이지와 OCR Block 조회 |
| `GET /api/v1/documents/{document_id}/pages/{page_id}/image` | 원문 PNG 조회 |

동일 문서의 동시 실행은 409, OCR 패키지 미설치는 503입니다. 모델 초기화/다운로드가
실패하면 작업 상태에 `failed / ocr_unavailable`이 남습니다. 그 외 실패는
`processing_failed`, 서버 재시작으로 중단된 작업은 `interrupted`로 표시합니다.
상태를 확인한 뒤 동일 POST를 호출하면 새 ingestion_id로 재시도합니다.

### 원문과 좌표

PyMuPDF가 PDF의 회전 메타데이터를 반영해 PNG를 생성합니다. OCR 자동 문서 회전/왜곡
보정은 꺼서 결과 좌표가 PNG와 일치하게 합니다. bbox는 `[x0,y0,x1,y1]`이며 각 값은
0~1입니다. 표시할 이미지의 너비/높이를 곱하면 화면 좌표가 됩니다.

현재는 OCR 텍스트 행을 순서대로 저장하며 `block_type=paragraph`는 임시 분류입니다.
구조 신뢰도는 0입니다. OCR 원문 자체에는 Layout 분류를 덮어쓰지 않습니다.
별도 구조 버전의 [배치 분석](#배치-분석과-검토)에서 제목·표 후보와 2단 읽기 순서를 제안합니다.
텍스트가 없는 페이지도 빈 blocks와 함께 저장합니다. OCR 완료는 검색 색인 완료나
내용 정확성 보장을 뜻하지 않습니다. 낮은 OCR 신뢰도는 그대로 반환해 검토할 수 있습니다.

### 버전과 실패 처리

새 OCR 작업은 항상 별도 ingestion과 이미지 폴더를 사용합니다. 전체 페이지 저장이
끝난 경우에만 document의 ingestion_id를 바꿉니다. 이전 Page/Block은 그대로 남기며
`pages?ingestion_id=...`로 과거 성공 버전을 조회할 수 있습니다. 이전 Chunk는 새 버전의
검색 결과에 섞이지 않도록 SQLite의 index_status를 failed로 바꿉니다.

실패한 작업의 부분 DB/이미지는 진단용으로 남지만 공개 페이지/이미지 API는 ready
버전만 제공합니다. 실패 파일 자동 정리, 취소, 자동 재시도는 후속 작업입니다.
작업 완료 시점에는 DB에 페이지 메타데이터와 실제 PNG가 모두 저장되어야 합니다.

### 운영 범위와 설정

- 현재는 단일 서버 프로세스용 `BackgroundTasks`입니다. `--workers`를 늘리지 마세요.
  시작 시 남아 있는 queued/running 작업을 중단 상태로 바꾸므로 여러 서버가 같은 DB를
  공유하려면 별도 작업 큐와 worker 소유권/lease를 먼저 구현해야 합니다.
- 개발 중 `--reload`로 서버가 재시작되면 진행 중 작업을 다시 요청해야 할 수 있습니다.
- `PAGE_STORE_PATH`: PNG 저장 위치, 기본 `data/pages`.
- `OCR_DPI`: 72~300, 기본 150. `OCR_MAX_PIXELS`: 페이지당 기본 25,000,000.
- `OCR_LANGUAGE`: `korean`(기본) 또는 `en`. CPU용 PP-OCRv5 mobile 검출 모델과
  언어별 mobile 인식 모델을 명시해 사용하며 CPU 스레드는 2개로 제한합니다.
- Windows CPU에서 확인된 oneDNN/PIR 연산 오류를 피하도록 `enable_mkldnn=False`로
  설정했습니다. CPU 추론 속도는 문서 크기와 PC 성능에 따라 달라집니다.
- `OCR_DETECTION_MODEL_DIR`, `OCR_RECOGNITION_MODEL_DIR`: 준비된 로컬 모델 디렉터리.
  생략하면 첫 실행 시 PaddleOCR가 공개 모델을 다운로드할 수 있습니다.
  지정하는 로컬 폴더는 선택된 mobile 검출/인식 모델과 호환되어야 합니다.
- PaddleX 캐시 경로는 `PADDLE_PDX_CACHE_HOME`으로 지정할 수 있습니다.
- `.env.example`은 예시이며 현재 앱이 `.env` 파일을 자동으로 읽지는 않습니다.

OCR 3.x의 `predict` 및 결과 필드는 [PaddleOCR 공식 문서](https://github.com/PaddlePaddle/PaddleOCR/blob/main/docs/version3.x/pipeline_usage/OCR.en.md)를 기준으로 연결했습니다.

### 테스트

기본 `python -m pytest -q`는 모델 다운로드 없이 실제 PDF 렌더링/DB/API와 fixture OCR을
검증합니다. 실제 엔진을 실행하는 선택 테스트는 다음과 같습니다.

```powershell
$env:RUN_OCR_LIVE="1"
$env:PADDLE_PDX_CACHE_HOME="$PWD/data/paddlex"
python -m pytest tests/test_ocr_live.py -q -s
```

Good/Medium/Poor 합성 스캔을 만들고 좋은 스캔에서 `DTC`를 인식하는지 확인합니다.
중간/낮은 품질에서는 결과 형식과 신뢰도 범위를 확인하며, 인식률 임계값은 강제하지 않습니다.
실제 한국어 표/업무 문서의 정량 정확도 평가는 별도의 대표 문서 집합이 필요합니다.

2026-09-30 검증: PaddleOCR 3.7.0 / PaddlePaddle 3.3.1 / Windows CPU에서
PP-OCRv5 mobile 검출 + 한국어 mobile 인식 모델로 선택 테스트를 통과했습니다.
세 품질 모두 합성 영문 `Check DTC before restart`를 인식했습니다. 이는 한글 인식률
검증 결과가 아니며, 실제 업무 자료의 품질 검증은 남아 있습니다.

---

## 문서 입력 및 OCR 검토

### 실행과 사용

`run.bat` 실행 후 <http://127.0.0.1:8000/>에 접속한다. FastAPI가 화면을 함께 제공하므로
Node 개발 서버나 인터넷 폰트가 필요 없다. 실제 OCR에는 기존처럼 `.[ocr]` 설치가 필요하다.

1. PDF를 선택하고 **PDF 등록**을 누른다. 동일 내용이면 기존 문서가 선택된다.
2. **OCR 시작**을 누른다. 저장된 페이지 수를 표시하며 처리 중에는 중복 실행을 막는다.
3. 완료 후 페이지별 이미지와 텍스트를 비교한다. 문장을 선택하면 원문 영역이 강조된다.
4. 작은 글씨는 **원본 크기로 보기**로 확인한다. **신뢰도 80% 미만만**은 검토 대상을
   좁히는 임시 기준이다. 80% 이상의 결과도 틀릴 수 있으며 정확도/합격 기준이 아니다.
5. 실패 원인을 확인하고 **OCR 다시 실행**을 누른다. 이전 성공 결과는 보존된다.

선택 문서는 URL에 기록된다. 새로고침하면 서버에서 최신 작업을 조회해 진행 상태를
복원한다. 문서의 `ready`와 최신 작업의 `failed`는 함께 존재할 수 있다. 이 경우 화면은
실패 이유와 이전 성공 결과를 표시한다. 다른 탭의 변경 사항은 새로고침으로 반영한다.
목록은 10개, 결과는 한 페이지씩 조회하며 장시간 요청과 조회 실패 안내를 제공한다.

아직 OCR 텍스트 수정, 검토 승인 저장, 표 구조 분석, 인수인계서 생성은 구현하지 않았다.
현재 목적은 원문과 결과를 확인하는 것이다. 단일 서버 프로세스 운영 제약도 동일하다.

2026-10-04부터 OCR 완료 후 **근거 준비**로 페이지별 텍스트 묶음을 저장하고
포함된 원문 영역을 함께 강조할 수 있다. [구조/근거 안내](#문서-구조chunk원문-근거-연결)를 참고한다.
이는 페이지 기본 구조이며 표/다단 분석이나 검색 완료를 뜻하지 않는다.

### 코드 읽기

- `src/handover_ai/main.py`: `/` 화면과 `/static` 자산 제공. data 폴더는 공개하지 않는다.
- `src/handover_ai/web/index.html`: 업로드, 문서 목록, 진행 상태, 원문/텍스트 비교 영역.
- `src/handover_ai/web/review.js`: API 호출·폴링·페이지 이동·강조 좌표·상태 복원.
  요청 세대 번호로 다른 문서의 늦은 응답을 무시하며 파일명과 OCR 내용은 textContent로 넣는다.
- `src/handover_ai/web/review.css`: 녹색 기반 화면과 모바일 세로 배치.
- `SQLiteOcrRepository.latest_job`: 초 단위 생성 시각이 같으면 rowid로 최신 작업을 결정한다.
- `scripts/validate_ocr.py`: 실제 OCR 실행, 페이지별 시간/텍스트/좌표/문자 오류율 기록.

### 실제 한국어 문서 검증

본문, 숫자/연락처, 표, 다단, 회전, 흐린 스캔 등 대표 자료를 준비한다. 정답은 사람이
원문을 읽고 PDF 페이지 순서대로 작성한 UTF-8 JSON 문자열 배열이다. 예:

```json
["첫 페이지의 정답 텍스트", "둘째 페이지의 정답 텍스트"]
```

```powershell
.\.venv\Scripts\python.exe scripts/validate_ocr.py --pdf C:/samples/work.pdf --reference C:/samples/reference.json --output data/review-work-01
```

`--reference`를 생략하면 시간과 인식 결과만 기록하고 오류율은 null이다. 출력 폴더는
매번 새 이름을 사용한다. 기존 결과를 덮어쓰지 않는다. PDF 원문·정답·평가 결과에는
업무 정보가 포함될 수 있으므로 Git에서 제외된 `data`에 보관한다.

`report.json`의 `cer`는 문자 추가·누락·대체 횟수를 정답 문자 수로 나눈 값이다.
한글 NFC 정규화 후 공백/줄바꿈을 제외하며 숫자와 문장부호는 포함한다. 값이 작을수록
좋지만 표 구조 품질·띄어쓰기·업무 적합성을 평가하는 지표는 아니다. 정답이 빈 페이지는
null로 두며 누락/불필요한 인식은 사람이 확인한다. 페이지별 숫자 오류와 읽기 순서도
별도로 기록하고 실제 문서에 맞는 합격 기준을 정한다.

### 합성 한국어 검증 결과 — 2026-10-03

실제 업무 PDF가 제공되지 않아 맑은 고딕으로 만든 개인정보 없는 본문·표·흐린 본문의
이미지 PDF를 사용했다. 실제 PaddleOCR 및 로컬 mobile 한국어 모델로 실행했다.

| 페이지 | 유형 | 공백 제외 문자 오류율 | OCR 시간 |
|---|---|---:|---:|
| 1 | 한국어 본문·전화번호·시간 | 0% | 6.984초 |
| 2 | 점검 항목 표 | 0% | 2.303초 |
| 3 | 흐림·낮은 대비 본문 | 0% | 2.387초 |

첫 페이지 시간은 모델 초기화를 포함한다. 렌더링은 페이지별 OCR 시간에서 제외된다.
단순 합성 자료 세 장의 결과이며 실제 한국어 업무 문서 품질 검증을 대체하지 않는다.
표의 글자 인식은 검증했으나 셀 구조 복원은 검증하지 않았다.
로컬 결과: `data/ocr-review-korean-verified/report.json`.

재현 예시(모델 캐시 경로는 자신의 환경에 맞게 변경):

```powershell
$env:PADDLE_PDX_CACHE_HOME="$PWD/data/ocr-validation/models"
$env:OCR_DETECTION_MODEL_DIR="$PWD/data/ocr-validation/models/official_models/PP-OCRv5_mobile_det"
$env:OCR_RECOGNITION_MODEL_DIR="$PWD/data/ocr-validation/models/official_models/korean_PP-OCRv5_mobile_rec"
.\.venv\Scripts\python.exe scripts/validate_ocr.py --synthetic --output data/review-korean-new
```

Windows 기본 글꼴 경로를 사용한다. 다른 OS는 `--font`로 한글 TTF를 지정한다.

### 회귀 검증

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check .
```

브라우저 테스트는 Playwright와 설치된 Edge를 사용한다. 서버를 새로 시작해야 빈 임시
DB에서 테스트한다. 테스트 서버의 제어 API는 운영 앱에 포함되지 않는다.

```powershell
.\.venv\Scripts\python.exe -m uvicorn review_server:app --app-dir tests --port 8765
# 별도 터미널에서 실행. 합성 검증 도구가 만든 PDF 경로를 지정한다.
$env:REVIEW_PDF="data/ocr-review-korean-verified/synthetic-korean.pdf"
node tests/review.browser.mjs
```

Playwright 모듈이 다른 위치에 있으면 `PLAYWRIGHT_MODULE`에 해당 모듈 경로/파일 URL을
지정한다. `BROWSER_CHANNEL=chrome`으로 브라우저를 바꿀 수 있다. 이 테스트는 OCR 엔진을
fixture로 대체하여 화면/서버 연결을 검증한다. 실제 엔진 품질은 위 평가 도구로 확인한다.

---

## 문서 구조·Chunk·원문 근거 연결

아래 설명은 기존 **페이지 순서** 방식이다. 2026-10-04부터 별도 버전의
**배치 분석 (시험)**도 제공한다. [배치 분석과 검토](#배치-분석과-검토)를 참고한다.

### 사용 방법

`run.bat` 실행 후 <http://127.0.0.1:8000/>에서 문서를 선택한다.
OCR 완료 후 **근거 준비**를 누르면 페이지별 근거 묶음을 저장한다.
하단 **이 페이지의 근거 묶음**에서 항목을 선택하면 해당 OCR 행의 원문 영역을 모두 강조한다.
한 페이지에 근거가 많으면 **근거 더 보기**로 추가 조회한다.

같은 버전에서 버튼을 다시 눌러도 중복 생성하지 않는다. 새로고침 후에도 결과가 남는다.
OCR 재처리에 성공하면 새 버전의 근거를 다시 준비한다. 재처리에 실패하면 이전 성공
버전의 근거를 계속 볼 수 있다. 다른 탭에서 OCR 버전이 바뀐 경우에는 새로고침 안내를 표시한다.

### 현재 규칙과 한계

- 규칙 버전: `page-lines-v1:chars=800`.
- 페이지마다 임시 Section을 만든다. 제목은 `1페이지`처럼 표시하며 실제 목차 추출 결과가 아니다.
- Section은 `title_source=inferred`, `structure_confidence=0`이다.
- OCR `reading_order`를 보존하고, 행 사이 줄바꿈을 포함하여 약 800자 단위로 묶는다.
- 페이지를 넘겨 묶지 않으며 원문 행을 중간에서 자르지 않는다. 단일 행이 800자를 넘으면
  해당 행만 하나의 긴 Chunk로 보존한다. 이는 소프트 제한이며 임베딩 토큰 제한은 아니다.
- 텍스트를 새로 쓰거나 요약하지 않는다. 공백/문장부호를 포함한 행 텍스트가 그대로 연결된다.
- 공백뿐인 행은 제외한다. 빈 페이지는 Section만 만들고 Chunk는 만들지 않는다.
- Chunk OCR 신뢰도는 포함된 행 중 최솟값이다. 평균이 저품질 행을 가리지 않도록 한다.
- 표의 셀 복원, 제목 판별, 다단 읽기 순서 교정은 후속 Layout 분석이다. 현재 순서는
  OCR 엔진이 저장한 순서이므로 복잡한 문서의 의미상 읽기 순서를 보장하지 않는다.
- 생성 Chunk의 `index_status`는 `pending`이다. 임베딩/검색은 아직 실행하지 않는다.

### 저장 구조와 버전 보존

```text
structure_run (문서 ID + ingestion_id + parser_version별 1개)
  └─ structure_section (Section ↔ 실행 버전 ↔ 원문 Page)
       ├─ section (페이지 기본 구조)
       └─ chunk
            └─ chunk_source → document_block → page → 원문 이미지
```

기존 `section`에 버전 열이 없으므로 별도 매핑 테이블로 버전을 명시한다. 기존 DB의
테이블/열을 바꾸지 않고 `CREATE TABLE IF NOT EXISTS`로 추가한다. 매핑이 없는 과거
Section은 신규 조회에서 제외하고 보존한다. `schema_version=1`은 기존 기반 스키마
버전이며, 새 구조 규칙은 `structure_run.parser_version`으로 관리한다.

`document_block.section_id`는 덮어쓰지 않는다. 같은 OCR 원문을 이후 다른 Layout
규칙으로 분석할 때 원문에 붙은 전역 Section 포인터를 바꾸면 과거 해석이 손상되기 때문이다.
새 Chunk의 Section과 매핑 테이블이 각 구조 버전의 소속을 나타낸다.

쓰기 예약(`BEGIN IMMEDIATE`) 후 현재 OCR 버전과 완료 상태를 다시 확인한다.
구조 실행 기록·Section·Chunk·원문 연결을 하나의 트랜잭션으로 저장한다. 중간 실패는
모두 롤백하고 반복/동시 요청은 같은 결과를 반환한다. 저장 중 외부 모델 호출은 없다.
원문 연결 시 실제 Block이 해당 Section의 페이지에 속하는지도 DB에서 확인한다.

ID는 문서/처리/규칙/페이지/순서에서 결정적으로 생성한다. 새 OCR 버전은 다른 ID를 얻는다.
OCR 재처리에 성공하면 기존 동작대로 예전 Chunk의 `index_status`는 `failed`가 되지만,
원문 근거 조회는 계속 허용한다. 이것은 과거 근거 보존이며 검색 가능하다는 뜻은 아니다.
생성 문서는 `evidence_id`, `chunk_id`, `ingestion_id`, `block_ids`를 고정하여 저장해야 한다.

### API

| 요청 | 의미 |
|---|---|
| `POST /api/v1/documents/{id}/structure` | 현재 OCR 버전의 구조/Chunk 생성 또는 기존 결과 반환 |
| `GET /api/v1/documents/{id}/structure` | 현재 OCR 버전의 구조 요약; 미생성이면 null |
| `GET /api/v1/documents/{id}/chunks` | 현재 OCR 버전의 Chunk 목록 |
| `GET /api/v1/documents/{id}/chunks/{chunk_id}` | 특정 Chunk와 원문 근거 조회; 과거 성공 버전도 허용 |

POST 본문은 `{"ingestion_id": "ING-..."}`이며 화면에서 확인한 버전을 필수로 전달한다.
OCR 미완료·버전 변경은 409, 없는 문서는 404, 잘못된 입력은 422다.
GET 구조/목록은 `ingestion_id`로 과거 버전을 지정할 수 있다. 다른 문서/실패 버전을
지정하면 구조는 null, Chunk는 빈 목록을 반환한다. 다른 문서 ID로 상세를 조회하면 404다.
목록은 `page_number`, `limit`(1~100), `offset`(0 이상)을 지원한다.

Chunk 응답에는 `evidence`, `sources`, `page_id`, `section_title`이 포함된다.
`sources`에는 원문 텍스트·신뢰도·0~1 bbox가 있고, 이미지는 기존 페이지 이미지 API로 조회한다.
`text_hash`는 저장 텍스트의 UTF-8 SHA-256이다.

### 코드와 검증

- `domain/chunks.py`: 순수 분할 규칙, 안정적인 ID, API 응답 계약.
- `ports/chunks.py`: 교체 가능한 저장소 계약.
- `services/chunks.py`: OCR 버전 고정, 페이지 읽기, 분할, 저장 조합.
- `adapters/chunk_repository.py`: 트랜잭션·버전 검증·조회·근거 조립.
- `api/routes/chunks.py`: 입력/응답과 오류를 HTTP로 변환.
- `web/review.js`: 준비 동작, 같은 버전의 페이지별 조회, 여러 원문 좌표 강조.

2026-10-04: 전체 테스트 49개 통과, 실제 OCR 선택 테스트 1개 건너뜀, Ruff 통과.
단위/API 테스트는 긴 행, 순서, 원문 보존, 빈 페이지, 중복/동시 요청, 저장 실패 롤백,
계산 도중 OCR 버전 변경, 다른 페이지 근거 차단, 과거 근거 보존, 기존 DB 추가 초기화를 검증한다.
Edge 브라우저에서 근거 준비/재진입, 복수 좌표 강조, OCR 실패/재처리 후 버전 분리,
원문 HTML 문자열의 안전한 표시와 데스크톱/모바일 레이아웃을 확인했다.
이번 기능은 저장된 OCR을 사용하므로 실제 엔진을 재실행하지 않았다.

---

## 배치 분석과 검토

### 사용 방법

1. `run.bat` 실행 후 문서를 선택하고 OCR을 완료한다.
2. **정리 방식**에서 **배치 분석 (시험)**을 선택한다.
3. **근거 준비**를 누르면 별도 배치 분석 버전이 생성된다.
4. **배치 분석 · 검토 필요**에서 영역과 사유를 확인한다. 영역 버튼은 원문 좌표를 강조한다.
5. **페이지 순서**로 전환하면 기존 방식의 결과와 비교할 수 있다. 전환은 결과를 삭제하지 않는다.

선택 방식은 URL에 남아 새로고침 후 복원된다. 분석 중에는 방식 선택을 잠시 막는다.
각 방식은 해당 OCR 버전에 고정되며 OCR 재처리 후에는 새 버전의 근거를 준비해야 한다.

### 구현 범위

`GeometryLayoutAnalyzer`는 저장된 OCR 텍스트·사각형을 읽는 규칙 기반 분석기다.
새 모델 다운로드 없이 동작한다. 모델 기반 Layout 분석기가 필요해지면 `LayoutAnalyzer`
포트를 구현하여 교체할 수 있다. 현재 기본 페이지 모드를 자동 변경하지 않는다.

| 대상 | 제안 규칙 | 표시와 한계 |
|---|---|---|
| 제목 | 독립된 짧은 행(80자 이하)의 높이가 중앙값의 1.5배 이상 | 제목 후보; 번호만으로 제목 판정하지 않음 |
| 본문 | 단일 열의 위→아래 순서 | 문장/텍스트는 그대로 보존 |
| 2단 | 페이지 폭의 4% 이상 수직 여백, 양쪽 최소 2행, 세로 범위 겹침 | 왼쪽 열→오른쪽 열 제안; 짧은 텍스트만 있으면 판별 보류 |
| 표 | 짧은 텍스트의 3열 이상이 3행 이상 정렬 | 표 후보; 행 우선 순서 제안, 셀/병합 관계는 복원하지 않음 |
| 짧은 2열 | 표/목록/다단 구분에 필요한 정보 부족 | 판별 보류, 기존 OCR 순서 유지 |
| 겹침/복잡한 배치 | 작은 사각형 면적의 30% 초과 겹침 또는 1,500행 초과 | 판별 보류, 기존 OCR 순서 유지 |
| 빈 페이지 | 유효한 OCR 텍스트 없음 | 빈 페이지/인식 누락 검토 안내 |

표 후보는 열 시작 좌표 차이 0.025 이내, 셀 텍스트 24자 이하, 행간 간격은 대표 글자
높이의 3배 이내라는 초기 규칙을 사용한다. 이 수치는 실제 문서의 정확도 평가로 정한
보편 기준이 아니다. 실제 자료에 맞춰 조정할 경우 `parser_version`을 올려야 한다.

가로로 전체 폭을 차지하는 제목/본문은 앞뒤 구간을 나누어 분석한다. 분류하지 못한
혼합 배치나 3단 본문은 원래 순서를 유지한다. 모든 결과는 `heuristic_layout` 검토
사유를 갖는다. 낮은 OCR 신뢰도는 별도 사유이며 Layout 정확도의 확률로 사용하지 않는다.

현재 페이지당 Section 하나를 유지하고, 첫 영역이 제목 후보이면 Section 제목에 표시한다.
중간 제목은 별도 근거 묶음으로 분리하지만 계층형 목차는 만들지 않는다. 표 구조 복원,
그림/캡션/각주 인식, 회전된 개별 글줄 교정, 사람의 검토 확정 저장은 후속 범위다.

### 버전과 원문 보존

- 기존 규칙: `page-lines-v1:chars=800`.
- 새 규칙: `geometry-layout-v1:chars=800`.
- 같은 OCR 버전에서 두 구조/Chunk가 병존하며 ID가 다르다.
- 영역 경계를 넘어 제목/본문/표 후보를 한 Chunk로 합치지 않는다. 영역 안에서는 기존
  800자 안팎의 분할 규칙을 재사용하고, 긴 단일 OCR 행은 자르지 않는다.
- 모든 공백 아닌 원문 Block이 정확히 한 영역에 한 번 나타나는지 검사한다.
- 영역 bbox가 소속 Block들의 실제 범위와 같은지 검사한다. 누락/중복/다른 페이지/잘못된
  좌표를 반환하는 분석기는 저장 전에 거절한다.
- DB 원문의 `reading_order`, 텍스트, bbox, 신뢰도, `section_id`는 변경하지 않는다.
- 새 `layout_page` 테이블에는 구조 ID별 영역·순서·검토 사유를 저장한다. 기존 DB는
  초기화 시 테이블만 추가하며 기존 테이블/근거는 유지한다.
- Layout 결과, Section, Chunk, 원문 연결은 한 트랜잭션이다. 실패 시 기본 방식까지
  손상시키지 않으며 신규 저장분만 롤백한다.
- OCR 재처리 후 기본 조회는 새 버전만 대상으로 하지만 과거 Chunk ID로 근거를 계속 조회할 수 있다.

### API

| 경로 | 동작 |
|---|---|
| `POST /api/v1/documents/{id}/layout` | 현재 OCR의 배치 분석 저장; 같은 요청은 기존 결과 반환 |
| `GET /api/v1/documents/{id}/layout` | 분석 요약, 검토 필요 페이지 수 |
| `GET /api/v1/documents/{id}/layout/pages` | 페이지별 영역·읽기 순서·검토 사유 |
| `GET /api/v1/documents/{id}/layout/chunks` | 배치 분석 버전의 근거 묶음 |

POST 본문은 `{"ingestion_id":"ING-..."}`다. 기존 `/structure` 동작은 유지된다.
GET은 `ingestion_id`로 과거 버전을 지정할 수 있다. pages/chunks는 `limit`(1~100),
`offset`을 지원하며 chunks는 `page_number`도 지원한다.
개별 Chunk는 기존 `/documents/{id}/chunks/{chunk_id}`로 두 방식 모두 조회한다.
요약의 `layout_mode=geometry_v1`, `review_page_count`와 Chunk의 `content_type`으로 구분한다.
페이지 응답의 `regions` 배열과 각 `block_ids` 순서가 제안 읽기 순서다.

### 검증 결과와 재현

2026-10-04 검증:

- 전체 테스트 66개 통과, 선택 OCR 테스트 1개 건너뜀, Ruff 검사 통과.
- `tests/fixtures/layout_cases.json`에 한국어 제목/본문, 제목·하단 본문을 포함한 2단,
  3열 표, 모호한 짧은 2열, 겹치는 인식 결과, 빈 페이지의 정답 순서와 분류를 저장했다.
- Golden fixture 6종은 사람이 구성한 OCR 좌표다. 실제 OCR 인식률 평가 자료는 아니다.
- API/DB 테스트로 버전 공존, 원문 불변, 과거 근거, 저장 실패 복구, 잘못된 분석 결과
  차단, 기존 DB 초기화 호환성을 검증했다.
- 실제 Edge 브라우저에서 방식 전환, 새로고침 복원, 검토 사유, 영역 강조,
  안전한 원문 텍스트 표시 및 데스크톱/모바일 레이아웃을 확인했다.
- 10월 3일 실제 PaddleOCR로 저장한 합성 한국어 3페이지 결과를 재분석했다.
  1·3페이지는 본문 영역 각 2개, 2페이지는 표 후보 1개로 분류됐다.
  원문 행이 누락/중복 없이 보존되는지 검사했다. OCR 모델을 재실행한 결과는 아니다.

저장된 OCR 결과를 재사용하는 명령:

```powershell
.\.venv\Scripts\python.exe scripts/validate_layout.py --ocr-report data/ocr-review-korean-verified/report.json --output data/layout-review-new
```

입력 보고서와 같은 폴더에 `validate_ocr.py`가 저장한 `pages/page-00001.png` 등의
이미지가 있어야 한다. 출력 폴더는 새 이름이어야 하며 앱 DB는 변경하지 않는다.
결과의 분류 수는 정확도 점수가 아니다. 실제 업무 문서의 정답 대비 정확도는 아직 미검증이다.

### 다음 단계

ChromaDB 검색과 페이지별 검토 기록 저장은 연결했다. 정답 대비 순서·분류 측정은
[배치 분석 평가 도구](evaluation.md#배치-분석-정답-평가)를 사용한다. 실제 문서 정답 작성·평가,
텍스트·순서 교정 편집과 표 셀 복원은 후속 과제다.

---

## 페이지별 배치 분석 검토 기록

2026-10-05 구현. 자동 분석의 제안과 사람이 원문을 대조한 기록을 구분한다.
OCR 텍스트·좌표·읽기 순서·Chunk·자동 경고는 검토 저장으로 변경되지 않는다.

### 화면 사용

1. OCR을 마친 문서에서 **배치 분석 (시험)**을 선택하고 근거를 준비한다.
2. 페이지 이미지와 텍스트, 영역 버튼으로 숫자·누락·읽기 순서·분류를 확인한다.
3. 세 항목을 모두 확인하면 **검토 완료 저장**을 누른다.
4. 문제가 있으면 메모에 내용을 쓰고 **수정 필요 저장**을 누른다.
5. 다시 검토할 때는 **미검토로 되돌리기**를 누른다. 이전 기록은 이력에 남는다.

페이지별 상태, 메모, 확인 항목, 저장 번호와 시간이 SQLite에 보존된다.
문서 전체의 완료·수정 필요·미검토 페이지 수도 표시한다. 화면은 최근 10개 이력을
보여 주며 API는 페이지를 나누어 전체 이력을 조회할 수 있다. 저장 시간은 UTC다.
메모는 2000자 이내이며 수정 필요 상태에는 공백이 아닌 메모가 필수다.

### 버전·동시 편집

기록의 키는 `(structure_id, page_id, revision)`이다. 페이지 순서 기본 방식에는
배치 분석 검토를 적용하지 않는다. OCR 재처리 후 만들어진 분석 결과는 새 키이므로
미검토로 시작한다. 과거 버전의 기록과 근거는 조회 가능하지만 새 검토 저장은 거절한다.

저장 요청에는 화면에서 읽은 `expected_revision`이 필요하다. 두 탭에서 동시에
수정하면 한 요청만 성공하고 나머지는 409를 받는다. 화면은 실패한 입력을 보존한다.
**저장 상태 다시 불러오기**는 입력을 서버의 최신 기록으로 교체하므로 필요한 메모를
먼저 보관하고 누른다. 네트워크 오류 후에도 이 버튼으로 실제 저장 여부를 확인한다.
최신 버전 검증, revision 비교, 이력 추가를 한 SQLite 쓰기 트랜잭션에서 실행한다.

### 검색 결과

검색 응답의 `human_review`에 최신 검토 상태·메모·확인 항목을 포함한다.
페이지 기본 방식은 `null`이다. 검토 변경만으로 벡터 재색인을 할 필요는 없다.
화면에서 저장하면 이전 검색 결과를 지우고, 다음 검색부터 새 기록을 표시한다.
자동 경고인 `review_required`/`review_reasons`는 사람이 확인해도 유지한다.
검색은 수정 필요/미검토 근거도 반환하며 이를 명시한다. 완료된 근거만 사용하도록 하는
초안 생성 정책은 이후 생성 단계에서 별도로 결정해야 한다.

### API

공통 경로: `/api/v1/documents/{document_id}/layout/{structure_id}`

| 메서드·경로 | 용도 |
|---|---|
| GET `/reviews` | 문서 전체 페이지 검토 집계 |
| GET `/pages/{page_id}/review` | 해당 버전 페이지의 최신 검토 기록 |
| PUT `/pages/{page_id}/review` | revision을 비교하여 새 기록 추가 |
| GET `/pages/{page_id}/review/history?limit=20&offset=0` | 최신순 이력 조회 |

PUT 예시:

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

존재하지 않거나 다른 문서/구조의 페이지는 404, 오래된 OCR/편집 충돌은 409,
완료 확인 항목 누락·수정 메모 누락·범위 오류는 422다.

### 검증 범위와 남은 작업

API/SQLite 테스트는 재조회·이력·집계·되돌리기·입력 검증·동시 저장·OCR 버전 격리·
다른 문서 연결 차단·재색인 없는 검색 반영을 확인한다.
브라우저는 저장·새로고침·다른 페이지 이동·검색 상태 표시·메모의 안전한 텍스트 출력을 검증한다.

현재 기록은 단일 로컬 작업자의 검토 결과다. 사용자 인증/검토자 신원 증명은 없다.
검토 완료는 내용의 정확성 보증이나 실제 업무 문서 평가 점수가 아니다.
실제 문서 정답 평가, 텍스트/읽기 순서 교정 편집기와 표 셀 복원은 후속 작업이다.
