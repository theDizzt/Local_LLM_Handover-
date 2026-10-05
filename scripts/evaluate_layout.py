"""저장 OCR 내보내기 → 수동 정답 작성 → 배치 분석 평가. DB는 읽기 전용이다."""

import argparse
import hashlib
import html
import json
import shutil
import sqlite3
from pathlib import Path

from handover_ai.adapters.layout import GeometryLayoutAnalyzer
from handover_ai.domain.layout_evaluation import AnnotatedPage, LayoutDataset, page_fingerprint
from handover_ai.domain.ocr import OcrBlock, OcrPage
from handover_ai.services.layout_evaluation import evaluate_layout


def read_document(database: Path, document_id: str, source_kind: str):
    # mode=ro는 오타 난 DB 경로에 새 DB를 만들거나 평가 도중 스키마를 변경하지 못하게
    # 한다. 한 읽기 트랜잭션에서 문서 포인터·페이지·Block을 같은 스냅샷으로 가져온다.
    connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("BEGIN")
        document = connection.execute(
            "SELECT d.*,i.status FROM document d JOIN ingestion i USING(ingestion_id) "
            "WHERE document_id=?",
            (document_id,),
        ).fetchone()
        if not document or document["status"] != "ready":
            raise ValueError("OCR이 완료된 문서 ID를 지정해 주세요.")
        pages, images = [], []
        rows = connection.execute(
            "SELECT * FROM page WHERE document_id=? AND ingestion_id=? ORDER BY pdf_page_number",
            (document_id, document["ingestion_id"]),
        ).fetchall()
        if [row["pdf_page_number"] for row in rows] != list(range(1, document["page_count"] + 1)):
            raise ValueError("OCR 페이지가 모두 저장되어 있지 않습니다.")
        for row in rows:
            blocks = connection.execute(
                "SELECT * FROM document_block WHERE page_id=? ORDER BY reading_order",
                (row["page_id"],),
            ).fetchall()
            page = OcrPage(
                **dict(row),
                blocks=[
                    OcrBlock(
                        block_id=block["block_id"],
                        reading_order=block["reading_order"],
                        text=block["text"],
                        bbox=json.loads(block["bbox_json"]),
                        confidence=block["ocr_confidence"],
                    )
                    for block in blocks
                ],
            )
            pages.append(AnnotatedPage(page=page, source_sha256=page_fingerprint(page)))
            image = Path(row["image_uri"])
            if not image.is_file():
                raise ValueError("원문 이미지 파일을 찾을 수 없습니다.")
            images.append(image)
        return LayoutDataset(
            dataset_id=document_id,
            document_id=document_id,
            ingestion_id=document["ingestion_id"],
            source_kind=source_kind,
            pages=pages,
        ), images
    finally:
        connection.close()


def read_fixtures(path: Path) -> LayoutDataset:
    # 이미 사람이 작성한 합성 fixture의 정답만 옮긴다. 분석기 예측으로 정답을
    # 자동 채우지 않으며, 일부 분류만 지정된 fixture는 그 작성률을 그대로 유지한다.
    cases = json.loads(path.read_text(encoding="utf-8"))
    pages = []
    for number, case in enumerate(cases, 1):
        page = OcrPage(
            page_id="FIXTURE-" + case["name"],
            ingestion_id="fixture-v1",
            pdf_page_number=number,
            width_px=1000,
            height_px=1000,
            render_dpi=150,
            rotation=0,
            blocks=[
                OcrBlock(block_id=key, text=text, bbox=box, confidence=0.99, reading_order=index)
                for index, (key, text, box) in enumerate(case["blocks"])
            ],
        )
        pages.append(
            AnnotatedPage(
                page=page,
                source_sha256=page_fingerprint(page),
                annotation_status="reviewed",
                expected_order=case["expected_order"],
                expected_kinds=case["expected_kinds"],
            )
        )
    return LayoutDataset(
        dataset_id="layout-fixtures-v1",
        document_id="synthetic-fixtures",
        ingestion_id="fixture-v1",
        source_kind="synthetic",
        pages=pages,
    )


def html_document(title: str, body: str) -> str:
    # OCR 문장과 식별자는 html.escape 후 넣는다. 문서에 들어 있는 HTML을 실행하지
    # 않으며 외부 스크립트·폰트·서버 없이 파일을 열어 검토할 수 있다.
    return (
        '<!doctype html><html lang="ko"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{html.escape(title)}</title><style>"
        "body{font-family:sans-serif;max-width:1100px;margin:32px auto;padding:0 20px;"
        "line-height:1.7;color:#20342c}table{border-collapse:collapse;width:100%}"
        "td,th{border:1px solid #ccd8cc;padding:8px;text-align:left;overflow-wrap:anywhere}"
        "img{max-width:100%;max-height:800px}pre{white-space:pre-wrap;overflow-wrap:anywhere}"
        "section{border-top:2px solid #ccd8cc;margin-top:32px}code{overflow-wrap:anywhere}"
        f"</style><h1>{html.escape(title)}</h1>{body}</html>"
    )


def write_dataset(dataset: LayoutDataset, output: Path, images: list[Path] | None = None):
    # 호출자가 모델 객체의 내부 목록을 수정했더라도 파일 쓰기 전에 계약을 재검증한다.
    dataset = LayoutDataset.model_validate(dataset.model_dump())
    if images is not None and len(images) != len(dataset.pages):
        raise ValueError("원문 이미지 수와 페이지 수가 다릅니다.")
    output.mkdir(parents=True, exist_ok=False)
    (output / "dataset.json").write_text(dataset.model_dump_json(indent=2), encoding="utf-8")
    sections = [
        "<p>dataset.json의 expected_order·expected_kinds를 원문과 비교하여 작성한 뒤 "
        "annotation_status를 reviewed로 바꾸세요. null은 미작성, 빈 배열은 빈 페이지 정답입니다. "
        "page와 source_sha256은 수정하지 마세요.</p>"
    ]
    for index, annotated in enumerate(dataset.pages):
        page = annotated.page
        sections.append(
            f"<section><h2>{page.pdf_page_number}페이지</h2>"
            f"<p>Page ID: <code>{html.escape(page.page_id)}</code></p>"
        )
        if images:
            image_name = f"page-{index + 1:05d}.png"
            shutil.copyfile(images[index], output / image_name)
            sections.append(f'<img src="{image_name}" alt="원문 {page.pdf_page_number}페이지">')
        sections.append("<table><tr><th>Block ID</th><th>OCR 문장</th><th>좌표</th></tr>")
        for block in page.blocks:
            sections.append(
                f"<tr><td>{html.escape(block.block_id)}</td>"
                f"<td>{html.escape(block.text)}</td><td>{block.bbox}</td></tr>"
            )
        sections.append("</table></section>")
    (output / "reference.html").write_text(
        html_document("배치 분석 정답 작성 자료", "".join(sections)), encoding="utf-8"
    )


def score(input_path: Path, output: Path):
    raw = input_path.read_bytes()
    dataset = LayoutDataset.model_validate_json(raw)
    report = evaluate_layout(dataset, GeometryLayoutAnalyzer())
    report["dataset_sha256"] = hashlib.sha256(raw).hexdigest()
    output.mkdir(parents=True, exist_ok=False)
    encoded = json.dumps(report, ensure_ascii=False, indent=2)
    (output / "evaluation.json").write_text(encoded, encoding="utf-8")
    def percent(value):
        return "평가 대상 없음" if value is None else f"{value * 100:.2f}%"

    coverage, order = report["coverage"], report["reading_order"]
    summary = (
        f"<p>자료 구분: {'합성 자료' if dataset.source_kind == 'synthetic' else '실제 문서'} · "
        f"평가 완료 {coverage['reviewed_pages']} / {coverage['pages']}페이지 · "
        f"미작성 {coverage['draft_pages']}페이지</p>"
        "<table><tr><th>지표</th><th>결과</th><th>평가 범위</th></tr>"
        f"<tr><td>분류 정답 작성률</td><td>{percent(coverage['classification_coverage'])}</td>"
        f"<td>{coverage['classified_blocks']} / {coverage['blocks']} Block</td></tr>"
        f"<tr><td>분류 일치율</td><td>{percent(report['classification']['accuracy'])}</td>"
        f"<td>정답이 있는 {coverage['classified_blocks']} Block</td></tr>"
        f"<tr><td>페이지 순서 완전 일치율</td><td>{percent(order['exact_page_accuracy'])}</td>"
        f"<td>{order['exact_pages']} / {order['nonempty_pages']}개 비어 있지 않은 페이지</td></tr>"
        f"<tr><td>두 Block의 선후 관계 일치율</td><td>{percent(order['pair_accuracy'])}</td>"
        f"<td>{order['pairs']}쌍 중 역전 {order['reversed_pairs']}쌍</td></tr></table>"
        "<h2>분류별 결과</h2><table><tr><th>분류</th><th>정답 수</th>"
        "<th>정밀도</th><th>재현율</th><th>F1</th></tr>"
    )
    for kind, metrics in report["classification"]["per_kind"].items():
        summary += (
            f"<tr><td>{html.escape(kind)}</td><td>{metrics['support']}</td>"
            f"<td>{percent(metrics['precision'])}</td><td>{percent(metrics['recall'])}</td>"
            f"<td>{percent(metrics['f1'])}</td></tr>"
        )
    summary += "</table>"
    page_details = html.escape(json.dumps(report["pages"], ensure_ascii=False, indent=2))
    body = (
        "<p>정답 작성 범위 내 점수입니다. 합성 자료 점수는 실제 업무 문서 품질을 뜻하지 않습니다. "
        "null은 평가할 정답이나 비교 대상이 없다는 뜻입니다.</p>"
        f"<h2>평가 요약</h2>{summary}"
        f"<h2>페이지별 비교</h2><pre>{page_details}</pre>"
    )
    (output / "evaluation.html").write_text(
        html_document("배치 분석 평가 결과", body), encoding="utf-8"
    )
    # 원문·메모를 콘솔에 복사하지 않는다. 원문이 필요한 정답 파일은 로컬 출력 폴더에 둔다.
    print(
        json.dumps(
            {
                "reviewed_pages": report["coverage"]["reviewed_pages"],
                "pair_accuracy": report["reading_order"]["pair_accuracy"],
                "classification_accuracy": report["classification"]["accuracy"],
            }
        )
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export", help="현재 OCR 원문과 이미지로 미작성 정답 자료 생성")
    export.add_argument("--database", type=Path, required=True)
    export.add_argument("--document-id", required=True)
    export.add_argument("--source-kind", choices=["synthetic", "real_document"], required=True)
    fixture = commands.add_parser("fixtures", help="기존 수동 합성 fixture를 평가 자료로 변환")
    fixture.add_argument("--input", type=Path, default=Path("tests/fixtures/layout_cases.json"))
    scoring = commands.add_parser("score", help="작성된 정답 범위만 평가")
    scoring.add_argument("--input", type=Path, required=True)
    for command in (export, fixture, scoring):
        command.add_argument("--output", type=Path, required=True, help="기존 자료와 다른 새 폴더")
    args = parser.parse_args()
    if args.command == "export":
        dataset, images = read_document(args.database, args.document_id, args.source_kind)
        write_dataset(dataset, args.output, images)
    elif args.command == "fixtures":
        write_dataset(read_fixtures(args.input), args.output)
    else:
        score(args.input, args.output)


if __name__ == "__main__":
    main()
