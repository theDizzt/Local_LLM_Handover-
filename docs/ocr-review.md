# 문서 입력 및 OCR 검토

## 실행과 사용

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
포함된 원문 영역을 함께 강조할 수 있다. [구조/근거 안내](document-structure.md)를 참고한다.
이는 페이지 기본 구조이며 표/다단 분석이나 검색 완료를 뜻하지 않는다.

## 코드 읽기

- `src/handover_ai/main.py`: `/` 화면과 `/static` 자산 제공. data 폴더는 공개하지 않는다.
- `src/handover_ai/web/index.html`: 업로드, 문서 목록, 진행 상태, 원문/텍스트 비교 영역.
- `src/handover_ai/web/review.js`: API 호출·폴링·페이지 이동·강조 좌표·상태 복원.
  요청 세대 번호로 다른 문서의 늦은 응답을 무시하며 파일명과 OCR 내용은 textContent로 넣는다.
- `src/handover_ai/web/review.css`: 녹색 기반 화면과 모바일 세로 배치.
- `SQLiteOcrRepository.latest_job`: 초 단위 생성 시각이 같으면 rowid로 최신 작업을 결정한다.
- `scripts/validate_ocr.py`: 실제 OCR 실행, 페이지별 시간/텍스트/좌표/문자 오류율 기록.

## 실제 한국어 문서 검증

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

## 합성 한국어 검증 결과 — 2026-10-03

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

## 회귀 검증

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
