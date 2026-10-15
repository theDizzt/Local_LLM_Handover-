"""로컬 평가 결과와 미실행 문항을 구분하여 집계한다."""

import argparse
import hashlib
import html
import json
import sqlite3
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path

from evaluate_real_documents import ROOT, evidence_matches, ordered_page_texts, save


def validate_report(output, total_questions):
    class Links(HTMLParser):
        def __init__(self):
            super().__init__()
            self.paths, self.details = [], 0

        def handle_starttag(self, tag, attrs):
            if tag == "details":
                self.details += 1
            if tag == "a":
                self.paths.extend(value for key, value in attrs if key == "href")

    parsed = Links()
    parsed.feed((output / "report.html").read_text(encoding="utf-8"))
    if parsed.details != total_questions:
        raise ValueError("HTML 문항 수 불일치")
    for path in parsed.paths:
        target = (output / path).resolve()
        if not target.is_relative_to(output.resolve()) or not target.is_file():
            raise ValueError("HTML 원문/보고서 링크가 유효하지 않습니다.")


def collect(output, dataset):
    documents = []
    connection = sqlite3.connect(
        (output / "evaluation.db").resolve().as_uri() + "?mode=ro", uri=True
    )
    connection.row_factory = sqlite3.Row
    try:
        for source in dataset["documents"]:
            code = source["document_id"]
            path = output / f"{code}.json"
            if not path.exists():
                path = output / f"{code}-progress.json"
            result = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            questions = []
            app_document = connection.execute(
                "SELECT document_id, ingestion_id FROM document WHERE file_sha256=?",
                (source["sha256"],),
            ).fetchone()
            blocks, page_texts, ocr_page_count = {}, {}, 0
            if app_document:
                ocr_page_count = connection.execute(
                    "SELECT COUNT(*) FROM page WHERE ingestion_id=?",
                    (app_document["ingestion_id"],),
                ).fetchone()[0]
                rows = connection.execute(
                    "SELECT b.*,p.pdf_page_number FROM document_block b JOIN page p USING(page_id) "
                    "WHERE p.ingestion_id=? ORDER BY p.pdf_page_number,b.reading_order",
                    (app_document["ingestion_id"],),
                ).fetchall()
                blocks = {row["block_id"]: dict(row) for row in rows}
                for row in rows:
                    n = row["pdf_page_number"]
                    page_texts[n] = page_texts.get(n, "") + "\n" + row["text"]
            block_pages = {key: b["pdf_page_number"] for key, b in blocks.items()}
            completed = {q["id"]: q for q in result.get("questions", [])}
            for gold in source["questions"]:
                q = completed.get(gold["id"])
                if not q:
                    questions.append({"id": gold["id"], "outcome": "not_run"})
                    continue
                retrieved = ordered_page_texts(
                    [b for hit in q["hits"] for b in hit["chunk"]["sources"]], block_pages
                )
                selected_items = (q["draft"]["result"] or {}).get("items", [])
                selected = ordered_page_texts([i["block"] for i in selected_items], block_pages)
                metadata_row = connection.execute(
                    "SELECT metadata_json FROM generation_run WHERE generation_run_id=?",
                    (q["draft"]["generation_run_id"],),
                ).fetchone()
                metadata = json.loads(metadata_row[0]) if metadata_row else {}
                candidates = ordered_page_texts(
                    [blocks[b] for b in metadata.get("source_block_ids", []) if b in blocks],
                    block_pages,
                )
                questions.append(
                    {
                        "id": q["id"],
                        "question": gold["question"],
                        "answerable": gold["answerable"],
                        "expected_answer": gold["expected_answer"],
                        "evidence": gold["evidence"],
                        "outcome": q["outcome"],
                        "error_code": q["draft"]["error_code"],
                        "seconds": q["seconds"],
                        "ocr_quote_exact": evidence_matches(gold, page_texts),
                        "retrieved_quote_exact": evidence_matches(gold, retrieved),
                        "candidate_quote_exact": evidence_matches(gold, candidates),
                        "selected_quote_exact": evidence_matches(gold, selected),
                        "all_gold_pages_retrieved": q["all_gold_pages_retrieved"],
                        "retrieved_pages": q["retrieved_pages"],
                        "selected_items": [
                            {
                                "page": block_pages[i["block"]["block_id"]],
                                "text": i["block"]["text"],
                                "category": i["category"],
                            }
                            for i in selected_items
                        ],
                        "source_guard": metadata.get("source_guard"),
                        "category_accuracy": None,
                    }
                )
            documents.append(
                {
                    "document_id": code,
                    "filename": source["filename"],
                    "pages": source["pages"],
                    "ocr_pages": ocr_page_count,
                    "image_directory": f"pages/{app_document['ingestion_id']}"
                    if app_document
                    else None,
                    "questions": questions,
                }
            )
    finally:
        connection.close()
    questions = [q for d in documents for q in d["questions"]]
    answered = [q for q in questions if q.get("answerable") is True]
    return {
        "gold_status": dataset["gold_status"],
        "documents": documents,
        "completed_ocr_pages": sum(d["ocr_pages"] for d in documents),
        "total_pdf_pages": sum(d["pages"] for d in documents),
        "outcomes": dict(Counter(q["outcome"] for q in questions)),
        "completed_questions": sum(q["outcome"] != "not_run" for q in questions),
        "total_questions": len(questions),
        "all_gold_pages_retrieved": sum(q["all_gold_pages_retrieved"] for q in answered),
        "answerable_evaluated": len(answered),
        "quote_coverage": {
            key: {
                "matched": sum(sum(q[key]) for q in answered),
                "total": sum(len(q[key]) for q in answered),
            }
            for key in (
                "ocr_quote_exact",
                "retrieved_quote_exact",
                "candidate_quote_exact",
                "selected_quote_exact",
            )
        },
    }


def write_report(output, report):
    save(output / "summary.json", report)
    lines = [
        "# 실제 문서 예비 평가",
        "",
        f"문항 실행: {report['completed_questions']}/{report['total_questions']}",
        "",
        "정답 파일은 최종 사용자 검토 전 초안이다. 생성 성공은 정답 판정이 아니다.",
        "인용문 포함률은 지정 페이지에서 공백 제외 완전 일치로 측정한다. "
        "표의 행·열 대응, 의미 정확도, 전체 페이지 CER와는 다르다.",
        "분류 정답과 읽기 순서·영역 정답이 제공되지 않아 해당 정확도는 측정하지 않았다.",
        "모든 문서를 한국어 OCR 기본 설정으로 처리한다. "
        "영문 문서 결과는 언어별 최적 성능이 아니다.",
        "미검토 근거를 포함하는 평가 전용 실행이며 사람 검토 완료로 저장하지 않았다.",
        "",
        "| 실행 결과 | 문항 수 |",
        "|---|---:|",
    ]
    lines += [f"| {key} | {value} |" for key, value in report["outcomes"].items()]
    lines += ["", "| 근거 인용문 포함 단계 | 일치 / 전체 |", "|---|---:|"]
    lines += [
        f"| {key} | {value['matched']} / {value['total']} |"
        for key, value in report["quote_coverage"].items()
    ]
    lines += ["", "| 문항 | 결과 | 오류 | 근거 페이지 검색 | 시간(초) |", "|---|---|---|---|---:|"]
    for document in report["documents"]:
        for q in document["questions"]:
            lines.append(
                f"| {q['id']} | {q['outcome']} | {q.get('error_code') or '-'} | "
                f"{q.get('all_gold_pages_retrieved', '-')} | {q.get('seconds', '-')} |"
            )
    (output / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    esc = html.escape
    sections = []
    for document in report["documents"]:
        sections.append(f"<h2>{esc(document['document_id'])} · {esc(document['filename'])}</h2>")
        for q in document["questions"]:
            sections.append(f"<details><summary>{esc(q['id'])} — {esc(q['outcome'])}</summary>")
            if "question" in q:
                sections.append(
                    f"<h3>{esc(q['question'])}</h3><p>정답 초안: {esc(q['expected_answer'])}</p>"
                )
                sections.append("<h4>제공된 근거</h4>")
                for evidence in q["evidence"]:
                    image = f"{document['image_directory']}/page-{evidence['pdf_page']:05d}.png"
                    sections.append(
                        f'<p><a href="{esc(image, quote=True)}">'
                        f"PDF {evidence['pdf_page']}쪽 원문</a>: "
                        f"{esc(evidence['quote'])}</p>"
                    )
                sections.append("<h4>실제 선택 원문</h4>")
                for item in q["selected_items"]:
                    sections.append(
                        f"<p>PDF {item['page']}쪽 · {esc(item['category'])}: "
                        f"{esc(item['text'])}</p>"
                    )
                if q["error_code"]:
                    sections.append(f"<p>실행 오류: {esc(q['error_code'])}</p>")
            sections.append("</details>")
    labels = {
        "answer_generated": "초안 생성(정확성 별도 검토)",
        "missed_answer": "유답 질문에서 근거 없음",
        "correct_abstention": "무답 질문 정상 거절",
        "false_answer": "무답 질문에 잘못 생성",
        "execution_error": "실행 오류",
        "not_run": "미실행",
    }
    overview = (
        f'<p class="count">{report["completed_questions"]} / '
        f"{report['total_questions']} 문항 실행</p>"
        "<p>최종 사용자 검토 전 정답 초안을 이용한 예비 평가입니다. "
        "생성 성공은 의미상 정답 확정을 뜻하지 않습니다.</p><table>"
        "<thead><tr><th>실행 결과</th><th>문항 수</th></tr></thead><tbody>"
        + "".join(
            f"<tr><td>{esc(labels[k])}</td><td>{v}</td></tr>" for k, v in report["outcomes"].items()
        )
        + "</tbody></table><p>지정 페이지의 인용문 포함 여부는 공백 제외 완전 일치입니다. "
        "표의 행·열 의미 대응이나 전체 OCR 문자 오류율은 아닙니다. "
        "분류·읽기 순서 정답은 없어 해당 정확도는 미측정입니다. "
        "한국어 OCR 기본 설정과 미검토 근거를 사용했습니다.</p>"
        '<p><a href="summary.md">상세 지표·문항별 표</a> · '
        '<a href="summary.json">집계 JSON</a></p>'
    )
    (output / "report.html").write_text(
        '<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" '
        'content="width=device-width, initial-scale=1"><title>실제 문서 예비 평가</title>'
        "<style>body{font:16px/1.65 system-ui,sans-serif;max-width:1000px;margin:40px auto;"
        "padding:0 20px;color:#17212b}table{border-collapse:collapse;width:100%}"
        "th,td{text-align:left;padding:10px;border-bottom:1px solid #ccd4df}"
        "th{background:#edf2f7}.count{font-size:24px;font-weight:700}"
        "details{border:1px solid #ccd4df;padding:14px;margin:12px 0}"
        "summary{cursor:pointer;font-weight:600}p{overflow-wrap:anywhere}a{color:#185abc}</style>"
        "<h1>실제 문서 예비 평가</h1>" + overview + "".join(sections) + "</html>",
        encoding="utf-8",
    )
    queue = ["# 생성된 초안의 내용 검토", "", "생성 성공 여부와 의미상 정답 여부를 구분한다.", ""]
    for document in report["documents"]:
        for question in document["questions"]:
            if question["outcome"] not in {"answer_generated", "false_answer"}:
                continue
            queue += [
                f"## {question['id']}",
                "",
                question["question"],
                "",
                f"정답 초안: {question['expected_answer']}",
                "",
                "실제 선택:",
                "",
            ]
            queue += [
                f"- PDF {item['page']}쪽 · {item['category']}: {item['text']}"
                for item in question["selected_items"]
            ]
            queue.append("")
    (output / "review-queue.md").write_text("\n".join(queue), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--require-complete", action="store_true")
    parser.add_argument(
        "--dataset",
        type=Path,
        default=ROOT / "data/evaluation-input/evaluation/questions_gold_draft.json",
    )
    args = parser.parse_args()
    manifest = json.loads((args.output / "manifest.json").read_text(encoding="utf-8"))
    if hashlib.sha256(args.dataset.read_bytes()).hexdigest() != manifest["dataset_sha256"]:
        raise ValueError("평가에 사용한 정답 파일과 다릅니다.")
    dataset = json.loads(args.dataset.read_text(encoding="utf-8-sig"))
    report = collect(args.output, dataset)
    if args.require_complete:
        if (
            report["completed_questions"] != report["total_questions"]
            or report["completed_ocr_pages"] != report["total_pdf_pages"]
        ):
            raise ValueError("전체 OCR 또는 문항 평가가 완료되지 않았습니다.")
        uri = (args.output / "evaluation.db").resolve().as_uri() + "?mode=ro"
        with sqlite3.connect(uri, uri=True) as connection:
            if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError("평가 DB 무결성 오류")
            if connection.execute("PRAGMA foreign_key_check").fetchall():
                raise ValueError("평가 DB 근거 연결 오류")
            for table in ("ocr_job", "search_run", "excerpt_draft"):
                if connection.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE status IN ('queued','running')"
                ).fetchone()[0]:
                    raise ValueError("진행 중인 평가 작업이 남아 있습니다.")
    write_report(args.output, report)
    validate_report(args.output, report["total_questions"])
    print(
        json.dumps(
            {
                "completed": report["completed_questions"],
                "outcomes": report["outcomes"],
                "ocr_pages": report["completed_ocr_pages"],
                "all_gold_pages_retrieved": report["all_gold_pages_retrieved"],
            }
        )
    )


if __name__ == "__main__":
    main()
