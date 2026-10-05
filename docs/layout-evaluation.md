# 배치 분석 정답 평가

저장된 OCR과 사람이 작성한 정답을 비교하는 도구다. 앱 DB를 읽기 전용으로 열고,
원문 이미지·텍스트·좌표·처리 버전을 새 폴더에 내보낸다. OCR/검색 모델 재실행이나
기존 자료 변경 없이 사용할 수 있다. 외부 서비스로 자료를 보내지 않는다.

## 실제 문서 준비

먼저 앱에서 PDF의 OCR을 완료한 뒤, 화면 URL의 `document` 값이나 문서 API에서
문서 ID를 확인한다. 아래 `DOC-ID`를 해당 ID로 바꾼다.

```powershell
.\.venv\Scripts\python.exe scripts/evaluate_layout.py export --database data/handover.db --document-id DOC-ID --source-kind real_document --output data/layout-gold-document1
```

합성 문서를 내보낼 때는 `--source-kind synthetic`을 사용한다. 도구가 실제 문서 여부를
자동 판정하지 않으므로 자료 성격에 맞게 지정해야 한다. 출력 폴더는 매번 새 이름을 쓴다.

- `dataset.json`: OCR 원문 스냅샷과 정답 입력란.
- `reference.html`: 페이지 원문 이미지, OCR 문장, Block ID와 좌표를 함께 보는 자료.
- `page-00001.png` 등: 정답 작성에 필요한 해당 버전의 원문 이미지 사본.

내보낸 자료에는 문서 원문이 포함된다. Git에서 제외된 `data` 안에서 관리한다.
내보내기는 DB 스냅샷을 읽으며 앱의 원문·색인·검토 기록을 수정하지 않는다.

## 수동 정답 작성

`reference.html`을 브라우저에서 열고 원문과 대조하면서 `dataset.json`의 각 페이지에서
아래 필드만 편집한다. `page`, `source_sha256`, 문서·ingestion ID는 수정하지 않는다.
읽기 순서를 모델 예측으로 채우지 않는다. 화면의 **검토 완료** 기록도 자동으로 정답으로
간주하지 않는다. 정확한 순서·분류 정답을 별도로 작성해야 한다.

```json
{
  "annotation_status": "reviewed",
  "expected_order": ["BLOCK-A", "BLOCK-C", "BLOCK-B"],
  "expected_kinds": {"BLOCK-A": "heading", "BLOCK-B": "paragraph"},
  "note": "두 단은 왼쪽 열 전체를 먼저 읽음"
}
```

위 예시는 페이지 안의 정답 필드만 나타낸 것이다. 내보낸 원문 필드는 유지한다.
Block ID는 실제 자료의 값을 사용한다.

- `annotation_status=draft`: 미작성/검토 중. 정답 필드가 있어도 점수에서 제외한다.
- `annotation_status=reviewed`: 사람이 작성한 정답을 평가에 포함한다.
- `expected_order=null`: 읽기 순서 미작성. 분류 정답만으로도 평가 가능하다.
- `expected_order=[]`: 글자가 없는 페이지의 명시적 정답.
- 읽기 순서를 작성하면 공백 행을 제외한 모든 Block을 정확히 한 번 포함해야 한다.
- `expected_kinds`: 일부 Block만 작성 가능. 제목 `heading`, 본문 `paragraph`,
  표 후보 `table_candidate`, 판별 보류 `unknown` 중 하나다.
- 표 후보 평가는 Block이 표 영역에 속하는지에 대한 분류이며 셀/병합/행열 복원 평가는 아니다.

## 평가 실행

```powershell
.\.venv\Scripts\python.exe scripts/evaluate_layout.py score --input data/layout-gold-document1/dataset.json --output data/layout-score-document1
```

`evaluation.html`은 표 형태의 결과와 페이지별 차이를 표시하고, `evaluation.json`은
자동 비교에 사용할 수 있는 전체 수치를 저장한다. JSON에는 자료/분석기 버전,
입력 파일 SHA-256, 문서·OCR ID, 정답 작성률, 오류 Block ID를 기록한다.

원문 해시가 달라진 자료, 다른 OCR 버전 혼합, 중복 페이지, 잘못된 Block ID,
읽기 순서의 누락·중복은 평가를 거절한다. 분석기가 원문 Block을 누락·중복하거나
원문 범위와 다른 좌표를 반환해도 평가 전체가 실패한다.

## 지표 해석

| 지표 | 계산 범위 |
|---|---|
| 분류 정답 작성률 | 검토 완료 페이지의 분류 정답 Block / 자료 전체의 공백 제외 Block |
| 분류 일치율 | 분류 정답이 있는 Block 중 예측과 정답이 같은 비율 |
| 분류별 정밀도·재현율·F1 | 분류 정답이 있는 Block만 대상으로 산출 |
| 페이지 순서 완전 일치율 | 순서 정답이 있는 비어 있지 않은 페이지 중 전체 순서가 같은 비율 |
| 선후 관계 일치율 | 순서 정답 내 모든 두 Block 쌍 중 상대 순서가 같은 비율 |

분모가 없으면 `null`/평가 대상 없음이다. 빈 페이지가 순서 점수를 높이지 않으며,
Block 1개 페이지는 완전 일치에는 포함되지만 선후 비교 쌍은 0개다.
선후 관계는 페이지별 백분율 평균이 아니라 전체 비교 쌍으로 가중 집계한다.
OCR 문자 정확도·OCR 인식 누락·제목 트리·표 셀 복원·검색 관련성은 이 도구의 평가 대상이 아니다.

## 합성 자료 회귀 결과

```powershell
.\.venv\Scripts\python.exe scripts/evaluate_layout.py fixtures --output data/layout-gold-fixtures-new
.\.venv\Scripts\python.exe scripts/evaluate_layout.py score --input data/layout-gold-fixtures-new/dataset.json --output data/layout-score-fixtures-new
```

기존 사람이 작성한 `tests/fixtures/layout_cases.json` 정답을 그대로 변환한다.
2026-10-05 실행 결과: 6페이지(빈 페이지 1개), 29 Block 중 분류 정답 14개(48.28%),
지정된 14개 분류 일치, 비어 있지 않은 5페이지 순서 완전 일치, 86개 선후 관계 일치.
이 자료는 규칙 개발에 사용한 회귀 fixture이므로 실제 업무 문서 일반화 성능을 뜻하지 않는다.
실제 문서 결과는 별도 `real_document` 자료로 기록해야 하며 아직 평가하지 않았다.

전체 테스트 97개 통과/선택 OCR 1개 건너뜀. DB 읽기 전용·정답 검증·의도적 오류의
점수 하락·미작성 제외·HTML 안전 출력과 실제 Edge의 데스크톱/모바일 보고서 표시를 확인했다.
