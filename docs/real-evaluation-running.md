# 실제 문서 예비 평가 실행

사용자가 제공한 8개 PDF(605페이지)와 40문항을 대상으로 기존 OCR·Layout·E5·Qwen
발췌 파이프라인을 실행한다. 정답은 `needs_human_review`인 초안이므로 결과도 예비 평가다.
평가 데이터와 PDF는 Git 제외 경로 `data/evaluation-input/`에 보존한다.

첫 전체 실행 결과와 수정 우선순위는 [2026-10-13 결과](real-evaluation-results-20261013.md)에 정리했다.

## 실행과 재개

```powershell
.\.venv\Scripts\python.exe scripts/evaluate_real_documents.py --output data/real-evaluation-20261012 --audit-only
.\.venv\Scripts\python.exe scripts/evaluate_real_documents.py --output data/real-evaluation-20261012
.\.venv\Scripts\python.exe scripts/report_real_evaluation.py --output data/real-evaluation-20261012
```

- `--audit-only`: 전체 PDF 해시·페이지 수·질문 ID·근거 페이지·유답/무답 계약을 검사한다.
- `--prepare-only`: OCR, 기본 구조와 Layout 구조, 전체 문서 검색 색인까지만 실행한다.
- `--documents DOC01 DOC03`: 지정한 문서만 처리한다. 없는 ID는 오류다.
- 동일 출력 폴더로 재실행하면 완료한 OCR·색인·문항 결과를 재사용한다.
- 중단으로 queued/running이 남았으면 **해당 출력 폴더를 쓰는 모든 평가 프로세스가
  종료된 것을 확인한 뒤** `--recover-interrupted`를 한 번 사용한다. 중단 작업을 실패로
  보존하고 재시도한다. 중단 OCR의 일부 페이지만 이어 붙이지 않고 새 처리 버전을 만든다.
- 다른 문서를 `--prepare-only --documents ...`로 나누어 실행할 수 있다. 같은 문서를
  동시에 처리하지 않으며 GPU 초안 생성은 한 프로세스에서 순차 실행한다.
- 정답 파일이나 평가 설정을 변경할 때는 새 출력 폴더를 사용한다.
  모델·프롬프트·코드를 변경한 비교 평가도 새 폴더로 구분한다.

평가 전용 `evaluation.db`, 원본 복사본, 페이지 PNG, Chroma 색인을 출력 폴더에 저장한다.
앱의 `data/handover.db`를 사용하지 않으며 사람 검토 완료 기록을 자동으로 만들지 않는다.
모델은 기존 로컬 경로를 사용하고 다운로드하지 않는다. 기본 한국어 OCR로 영문 문서도
처리하므로 영문 결과는 이 기본 구성에 대한 측정이지 영어 OCR 최적 설정의 성능이 아니다.

## 결과 파일

- `input-audit.json`: 원본 해시·페이지와 PDF 내장 텍스트의 근거 인용문 일치 검사.
  스캔 페이지의 내장 텍스트 부재는 정답 오류를 뜻하지 않는다.
- `DOCxx-ocr.json`: 페이지별 OCR 텍스트.
- `DOCxx-prepared.json`: 등록·OCR·기본 구조·Layout 구조 ID.
- `DOCxx-progress.json`: 완료한 문항별 체크포인트.
- `DOCxx.json`: 문서의 모든 질문을 실행한 결과. 검색 원문과 초안·오류·시간 포함.
- `summary.json`, `summary.md`, `report.html`: 보고서 스크립트로 최신 결과를 집계.
  HTML에서는 문항별 정답 초안·제공 근거·실제 선택 원문을 펼쳐 비교할 수 있다.

## 지표를 읽는 방법

| 지표 | 의미 |
|---|---|
| `ocr_quote_exact` | 정답의 지정 페이지 OCR에 인용문이 공백 제외 완전 일치로 포함되는지 |
| `retrieved_quote_exact` | 상위 5개 검색 Chunk의 원문에 인용문이 포함되는지 |
| `candidate_quote_exact` | 길이·개수·검토 정책 적용 뒤 모델에 전달할 후보에 포함되는지 |
| `selected_quote_exact` | 최종 선택 원문에 인용문이 포함되는지 |
| `all_gold_pages_retrieved` | 정답의 모든 근거 페이지가 상위 5개 검색 결과에 있는지 |
| `answer_generated` | 유답 질문에서 발췌 초안 생성. 의미상 정답 확정은 아님 |
| `missed_answer` | 유답 질문인데 관련 근거 없음으로 처리됨 |
| `correct_abstention` | 무답 질문을 관련 근거 없음으로 처리함 |
| `false_answer` | 무답 질문에 원문을 선택함 |
| `execution_error` | 시간 초과·모델/검색 오류 등. 무답 성공으로 세지 않음 |

근거 포함률 계산에서는 검색 순위와 무관하게 원문의 읽기 순서를 사용하고 중복 Block을
제거한다. 표 행·열의 의미 대응이나 OCR 문자 오류율을 측정하는 지표는 아니다.
페이지를 찾았더라도 답이 있는 행을 가져오지 못할 수 있다. 정상 생성된 초안도 누락·관계
오해·불필요 인용은 별도 검토해야 한다.

현재 정답에는 초안 분류와 전체 페이지 읽기 순서/영역 정답이 없으므로 해당 정확도는
미측정이다. 실행 시간에는 검색·모델 로딩·추론·작업 회수가 포함된다. 120초는 모델 대기
제한이며, 전체 문항 소요 시간이 이를 약간 넘을 수 있다.
