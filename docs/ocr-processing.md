# PDF 렌더링 및 OCR

## 실행

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

## 원문과 좌표

PyMuPDF가 PDF의 회전 메타데이터를 반영해 PNG를 생성합니다. OCR 자동 문서 회전/왜곡
보정은 꺼서 결과 좌표가 PNG와 일치하게 합니다. bbox는 `[x0,y0,x1,y1]`이며 각 값은
0~1입니다. 표시할 이미지의 너비/높이를 곱하면 화면 좌표가 됩니다.

현재는 OCR 텍스트 행을 순서대로 저장하며 `block_type=paragraph`는 임시 분류입니다.
구조 신뢰도는 0입니다. OCR 원문 자체에는 Layout 분류를 덮어쓰지 않습니다.
별도 구조 버전의 [배치 분석](layout-analysis.md)에서 제목·표 후보와 2단 읽기 순서를 제안합니다.
텍스트가 없는 페이지도 빈 blocks와 함께 저장합니다. OCR 완료는 검색 색인 완료나
내용 정확성 보장을 뜻하지 않습니다. 낮은 OCR 신뢰도는 그대로 반환해 검토할 수 있습니다.

## 버전과 실패 처리

새 OCR 작업은 항상 별도 ingestion과 이미지 폴더를 사용합니다. 전체 페이지 저장이
끝난 경우에만 document의 ingestion_id를 바꿉니다. 이전 Page/Block은 그대로 남기며
`pages?ingestion_id=...`로 과거 성공 버전을 조회할 수 있습니다. 이전 Chunk는 새 버전의
검색 결과에 섞이지 않도록 SQLite의 index_status를 failed로 바꿉니다.

실패한 작업의 부분 DB/이미지는 진단용으로 남지만 공개 페이지/이미지 API는 ready
버전만 제공합니다. 실패 파일 자동 정리, 취소, 자동 재시도는 후속 작업입니다.
작업 완료 시점에는 DB에 페이지 메타데이터와 실제 PNG가 모두 저장되어야 합니다.

## 운영 범위와 설정

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

## 테스트

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
