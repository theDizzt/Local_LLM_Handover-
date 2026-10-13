# 평가: OCR·배치 분석·실제 문서

품질 측정 도구와 결과를 모은다. 모든 평가 자료·결과는 업무 정보가 포함될 수 있으므로 Git에서 제외된
`data/` 아래에 두고, 출력 폴더는 매번 새 이름을 쓴다. 외부 서비스로 자료를 보내지 않는다.
**합성 자료 결과와 실제 문서 결과는 항상 구분해 기록한다.**

| 평가 | 도구 | 측정하는 것 | 상태 |
|---|---|---|---|
| OCR 문자 정확도 | `scripts/validate_ocr.py` | 페이지별 문자 오류율(CER)·시간 | 합성 3페이지만 측정 |
| 배치 분석 | `scripts/evaluate_layout.py` | 분류 정밀도/재현율, 읽기 순서 | 합성 fixture만 측정, 실제 정답 미작성 |
| 초안 (합성) | `scripts/validate_local_qwen.py` | 원문 선택·근거 없음 처리 | [프롬프트 버전 이력](search-and-draft.md#qwen-프롬프트-버전-이력-합성-자료) |
| 초안 (실제 문서) | `scripts/evaluate_real_documents.py` | 단계별 정답 인용 포함률, 무답 처리, 시간 | 8개 PDF·40문항 예비 평가 완료 |

---

## 1. OCR 문자 정확도

본문, 숫자/연락처, 표, 다단, 회전, 흐린 스캔 등 대표 자료를 준비한다. 정답은 사람이 원문을 읽고 PDF 페이지
순서대로 작성한 UTF-8 JSON 문자열 배열이다: `["첫 페이지 정답 텍스트", "둘째 페이지 정답 텍스트"]`.

```powershell
.\.venv\Scripts\python.exe scripts/validate_ocr.py --pdf C:/samples/work.pdf --reference C:/samples/reference.json --output data/review-work-01
```

`--reference`를 생략하면 시간과 인식 결과만 기록하고 오류율은 null이다. `report.json`의 `cer`는 문자
추가·누락·대체 횟수를 정답 문자 수로 나눈 값이다(한글 NFC 정규화, 공백/줄바꿈 제외, 숫자·문장부호 포함).
표 구조·띄어쓰기·업무 적합성은 평가하지 않는다.

**합성 한국어 결과 (2026-10-03)** — 맑은 고딕으로 만든 개인정보 없는 이미지 PDF, 실제 PaddleOCR·로컬 한국어 mobile 모델:

| 페이지 | 유형 | 공백 제외 CER | OCR 시간 |
|---|---|---:|---:|
| 1 | 본문·전화번호·시간 | 0% | 6.984초 (모델 초기화 포함) |
| 2 | 점검 항목 표 | 0% | 2.303초 |
| 3 | 흐림·낮은 대비 본문 | 0% | 2.387초 |

표의 글자 인식만 검증했고 셀 구조 복원은 검증하지 않았다. 결과: `data/ocr-review-korean-verified/report.json`. 재현:

```powershell
$env:PADDLE_PDX_CACHE_HOME="$PWD/data/ocr-validation/models"
$env:OCR_DETECTION_MODEL_DIR="$PWD/data/ocr-validation/models/official_models/PP-OCRv5_mobile_det"
$env:OCR_RECOGNITION_MODEL_DIR="$PWD/data/ocr-validation/models/official_models/korean_PP-OCRv5_mobile_rec"
.\.venv\Scripts\python.exe scripts/validate_ocr.py --synthetic --output data/review-korean-new
```

Windows 기본 글꼴을 사용한다. 다른 OS는 `--font`로 한글 TTF를 지정한다.

---

## 2. 배치 분석 정답 평가

앱 DB를 읽기 전용으로 열어 원문 이미지·텍스트·좌표·처리 버전을 내보내고, 사람이 작성한 정답과 비교한다.

### 내보내기

앱에서 OCR을 완료한 뒤 화면 URL의 `document` 값이나 문서 API에서 문서 ID를 확인한다.

```powershell
.\.venv\Scripts\python.exe scripts/evaluate_layout.py export --database data/handover.db --document-id DOC-ID --source-kind real_document --output data/layout-gold-document1
```

합성 문서는 `--source-kind synthetic`. 출력은 `dataset.json`(OCR 스냅샷과 정답 입력란),
`reference.html`(원문 이미지·OCR 문장·Block ID·좌표), `page-00001.png` 등이다.

### 정답 작성

`reference.html`을 보며 `dataset.json` 각 페이지의 아래 필드만 편집한다. `page`, `source_sha256`, 문서·ingestion ID는
수정하지 않는다. 모델 예측이나 화면의 **검토 완료** 기록을 정답으로 간주하지 않는다.

```json
{
  "annotation_status": "reviewed",
  "expected_order": ["BLOCK-A", "BLOCK-C", "BLOCK-B"],
  "expected_kinds": {"BLOCK-A": "heading", "BLOCK-B": "paragraph"},
  "note": "두 단은 왼쪽 열 전체를 먼저 읽음"
}
```

- `annotation_status`: `draft`는 점수에서 제외, `reviewed`만 평가에 포함.
- `expected_order`: `null`은 미작성, `[]`는 글자 없는 페이지. 작성하면 공백 행 제외 모든 Block을 정확히 한 번 포함.
- `expected_kinds`: 일부만 작성 가능. `heading` / `paragraph` / `table_candidate` / `unknown`.
  표 후보는 Block이 표 영역에 속하는지의 분류이며 셀/행열 복원 평가는 아니다.

### 실행과 지표

```powershell
.\.venv\Scripts\python.exe scripts/evaluate_layout.py score --input data/layout-gold-document1/dataset.json --output data/layout-score-document1
```

`evaluation.html`(표·페이지별 차이)과 `evaluation.json`(전체 수치, 버전, 입력 SHA-256, 정답 작성률, 오류 Block ID)을
만든다. 원문 해시 변경, OCR 버전 혼합, 중복 페이지, 잘못된 Block ID, 순서 누락·중복, 분석기의 Block 누락·중복·
좌표 오류는 평가를 거절한다.

| 지표 | 계산 범위 |
|---|---|
| 분류 정답 작성률 | 검토 완료 페이지의 분류 정답 Block / 전체 공백 제외 Block |
| 분류 일치율 | 분류 정답이 있는 Block 중 예측 = 정답 비율 |
| 분류별 정밀도·재현율·F1 | 분류 정답이 있는 Block만 대상 |
| 페이지 순서 완전 일치율 | 순서 정답이 있는 비어 있지 않은 페이지 중 전체 순서가 같은 비율 |
| 선후 관계 일치율 | 모든 두 Block 쌍 중 상대 순서가 같은 비율(전체 쌍 가중, O(n log n)) |

분모가 없으면 `null`이다. 빈 페이지는 순서 점수를 높이지 않는다. OCR 문자 정확도·제목 트리·표 셀·검색 관련성은 대상이 아니다.

**합성 회귀 결과 (2026-10-05)** — `tests/fixtures/layout_cases.json`을 변환:

```powershell
.\.venv\Scripts\python.exe scripts/evaluate_layout.py fixtures --output data/layout-gold-fixtures-new
.\.venv\Scripts\python.exe scripts/evaluate_layout.py score --input data/layout-gold-fixtures-new/dataset.json --output data/layout-score-fixtures-new
```

6페이지(빈 1), 29 Block 중 분류 정답 14개(48.28%) 모두 일치, 비어 있지 않은 5페이지 순서 완전 일치, 선후 관계 86쌍 일치.
규칙 개발에 쓴 fixture이므로 일반화 성능이 아니다. **실제 문서 정답은 아직 작성되지 않았다.**

---

## 3. 실제 문서 평가 (준비·실행)

추가 모델 설치 없이 현재 Qwen3-4B·E5로 평가한다. 평가 입력은 Git 제외 경로 `data/evaluation-input/`에 둔다.
2026-10-13 기준 PDF 8개·40문항이 준비되어 있으며 다시 만들 필요 없다.

### 새 자료를 추가할 때

1. 대표 PDF를 `data/evaluation-input/`에 둔다. 본문 중심 문서와 표/스캔 문서를 포함하면 좋다.
2. 문서마다 질문 3~5개와 정답 페이지(**PDF에서 몇 번째 페이지인지**)·원문 문장을 적는다.
3. 문서마다 답이 없는 질문 1개를 넣고 `문서에 없음`이라고 적는다.

```text
파일: 운영절차.pdf
질문: 장애가 발생하면 누구에게 연락하나요?
정답 페이지: 3
정답 원문: 장애 발생 시 운영 담당자에게 연락한다.
분류: 문제 해결

파일: 운영절차.pdf
질문: 출장비 지급일은 언제인가요?
정답: 문서에 없음
```

분류는 개요 / 사전 조건 / 절차 / 주의사항 / 문제 해결 중 하나다. 여러 문장이 정답이면 모두 적고,
원문에 없는 내용을 정답으로 추가하지 않는다. Block ID나 JSON은 직접 쓸 필요 없다.

### 실행과 재개

```powershell
.\.venv\Scripts\python.exe scripts/evaluate_real_documents.py --output data/real-evaluation-20261012 --audit-only
.\.venv\Scripts\python.exe scripts/evaluate_real_documents.py --output data/real-evaluation-20261012
.\.venv\Scripts\python.exe scripts/report_real_evaluation.py --output data/real-evaluation-20261012
```

- `--audit-only`: PDF 해시·페이지 수·질문 ID·근거 페이지·유답/무답 계약 검사.
- `--prepare-only`: OCR, 기본·Layout 구조, 검색 색인까지만 실행. `--documents DOC01 DOC03`: 지정 문서만.
- 같은 출력 폴더로 재실행하면 완료한 OCR·색인·문항 결과를 재사용한다.
- 중단으로 queued/running이 남았으면 **그 폴더를 쓰는 모든 평가 프로세스가 끝난 것을 확인한 뒤**
  `--recover-interrupted`를 한 번 쓴다. 중단 OCR은 이어 붙이지 않고 새 처리 버전을 만든다.
- 같은 문서를 동시에 처리하지 않으며 GPU 초안 생성은 한 프로세스에서 순차 실행한다.
- 정답·설정·모델·프롬프트·코드를 바꾼 비교 평가는 **새 출력 폴더**를 쓴다.

평가 전용 `evaluation.db`, 원본 복사본, 페이지 PNG, Chroma 색인을 출력 폴더에 저장한다. 앱의 `data/handover.db`를
쓰지 않으며 사람 검토 완료 기록을 자동으로 만들지 않는다. 기본 한국어 OCR로 영문 문서도 처리한다.

### 결과 파일

- `input-audit.json`: 원본 해시·페이지와 PDF 내장 텍스트의 인용문 일치 검사(스캔 페이지는 내장 텍스트가 없을 수 있다).
- `DOCxx-ocr.json`, `DOCxx-prepared.json`, `DOCxx-progress.json`(문항 체크포인트), `DOCxx.json`(검색 원문·초안·오류·시간).
- `summary.json`, `summary.md`, `report.html`: 집계. HTML에서 정답 초안·제공 근거·실제 선택 원문을 펼쳐 비교한다.
- `review-queue.md`: 생성된 초안의 사람 검토 목록.

### 지표

| 지표 | 의미 |
|---|---|
| `ocr_quote_exact` | 정답 페이지 OCR에 인용문이 공백 제외 완전 일치로 포함 |
| `retrieved_quote_exact` | 상위 5개 검색 Chunk 원문에 포함 |
| `candidate_quote_exact` | 길이·개수·검토 정책 적용 후 모델 후보에 포함 |
| `selected_quote_exact` | 최종 선택 원문에 포함 |
| `all_gold_pages_retrieved` | 정답의 모든 근거 페이지가 상위 5개 검색 결과에 있음 |
| `answer_generated` | 유답 질문에서 초안 생성(의미상 정답 확정 아님) |
| `missed_answer` | 유답인데 관련 근거 없음 처리 |
| `correct_abstention` | 무답을 관련 근거 없음으로 처리 |
| `false_answer` | 무답에 원문을 선택 |
| `execution_error` | 시간 초과·모델/검색 오류(무답 성공으로 세지 않음) |

포함률은 원문 읽기 순서로 중복 Block을 제거해 계산한다. 표 행·열 대응이나 CER은 측정하지 않는다.
실행 시간에는 검색·모델 로딩·추론·작업 회수가 포함되며 120초는 모델 대기 제한이다.

---

## 4. 실제 문서 예비 평가 결과 (2026-10-12~13)

PDF 8개·605페이지, 질문 40개(유답 32, 무답 8)로 PaddleOCR → Layout → E5/Chroma → Qwen3-4B(v9) 발췌 파이프라인을
평가했다. 첫 실제 문서 기준선이며 **운영 품질 합격이 아니다.**

| 대상 | 결과 | 문항 수 |
|---|---|---:|
| 유답 | 발췌 초안 생성(정확성 별도 검토) | 15 |
| 유답 | 관련 근거 없음으로 처리 | 12 |
| 유답 | 모델 시간 초과 | 5 |
| 무답 | 정상 거절 | 7 |
| 무답 | 무관한 원문 선택 | 1 |

정답 인용문 49개의 공백 제외 완전 일치 포함 수: **OCR 37 → 검색 상위 5개 21 → 모델 후보 12 → 최종 선택 2.**
정답률이 아니며 아래 한계와 함께 읽어야 한다. 정답의 모든 근거 페이지를 검색한 유답 문항은 26/32개다.

결과 자료(`data/real-evaluation-20261012/`): `report.html`(문항별), `summary.md`, `summary.json`, `review-queue.md`.
PDF 해시·페이지 수가 정답 목록과 일치했고, 원본 자료·질문·앱 DB·검토 기록은 변경하지 않았다.

### 확인된 결함과 수정 순서

1. **문장·문단 단위 근거 묶음과 표 행·열 관계 보존**
   - DOC05-Q01은 4단계 중 첫 두 단계가 있는 OCR 한 줄만 선택했다. DOC06-Q02는 다섯 번째 단계 뒷부분, DOC06-Q04는
     줄바꿈 이후 핵심 조치, DOC08-Q03은 세 유형 중 앞줄을 누락했다.
   - 원인: 모델 입력이 Chunk를 개별 OCR Block으로 풀고 각 Block을 독립 검증한다. 연결된 문장/문단을 선택·검증
     단위로 만들되 원래 Block ID·좌표·원문과 과거 구조 버전은 보존한다.
   - 표는 머리글·행 이름·값이 같은 묶음에 들어가게 하고 DOC02·DOC07 표 문항으로 회귀 검증한다.
2. **질문 조건 충족 검증**
   - DOC07-Q05(문서에 없는 특정 병원의 2026년 방문 인원)에서 30쪽의 맥락 없는 숫자 `13`을 선택했다.
   - DOC03-Q01은 목적 대신 복구 절차 일부를, DOC05-Q02는 분석 활동 대신 단계 목록을, DOC08-Q04는 세척 시험 방법
     대신 일반 상태조사 문장을 선택했다.
   - 주제 언급과 질문의 대상·시점·수량·행위 충족을 구분해야 한다. 숫자 Block 일괄 차단은 정상 표 답변을 없애므로
     행·열 문맥과 함께 검증한다.
3. **검색 누락과 후보 제한 분리 개선** — DOC03-Q03은 정답 페이지가 상위 5개에 없었고, DOC01-Q04는 필요한 인용문
   일부가 Chunk에 없었다. 검색에는 있었으나 후보 제한에서 빠진 경우도 있다(`retrieved_` vs `candidate_quote_exact`).
4. **숫자 교정과 출처 보존** — DOC06-Q03 선택 원문에 `1,000RLU`와 OCR 오류 `1,00RLU`가 함께 있었다(8쪽 이미지로 확인).
   교정은 원문을 보존한 새 revision으로 저장해야 하며, 모델이 값을 추정해 고치는 방식은 채택하지 않는다.
5. **시간 초과** — DOC01-Q01~Q04, DOC04-Q01이 120초 제한에 걸렸다. 다른 앱의 GPU 메모리 사용도 있었으나 단독 원인으로
   단정하지 않았다. 입력 길이·독립 검증 시간·GPU 메모리를 분리해 재시험한다.

### 정답과 지표의 한계

- 질문 파일은 `needs_human_review` 상태다. 정답 초안의 의미를 사람이 최종 승인하지 않았으므로 생성 성공 수는 정답률이 아니다.
- DOC03-Q01 인용문은 `destroyed`지만 PDF 2쪽 원문은 `destoryed`(원래 오타)이고 OCR도 그대로 읽었다. OCR 오류가 아니다.
  원문 인용과 의미상 정답을 분리해 교정할 필요가 있다.
- 공백만 제거한 완전 일치라 문장부호·대시·단위 문자 차이도 불일치다. 여러 줄·표 인용에 한계가 있다.
- DOC06-Q03처럼 같은 사실이 다른 페이지에 반복되면 유효한 대체 근거가 엄격한 페이지 일치에서 빠질 수 있다.
- 분류 정답과 전체 페이지 읽기 순서·영역 정답이 없어 분류·Layout 정확도는 미측정이다. 605페이지 전체를 사람이 검토하지 않았다.
- 검증: 자동 테스트 161개 통과/선택 실모델 4개 건너뜀, Ruff 통과. HTML 링크는 파일 검사만 했고 브라우저 시각 검증은 미완료다.
