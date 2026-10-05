"""validate_ocr.py가 저장한 결과를 재사용하여 배치 분석을 검토한다.

OCR 모델을 다시 실행하거나 앱 DB를 수정하지 않는다. 실제 인식 좌표와 순서는 입력
report.json에서 읽고, 분류/검토 사유를 별도 새 폴더에 기록한다. 정답과 비교하는 도구가
아니므로 출력된 영역 수를 정확도 점수로 해석하면 안 된다.
"""

import argparse
import json
from collections import Counter
from pathlib import Path

import pymupdf

from handover_ai.adapters.layout import GeometryLayoutAnalyzer
from handover_ai.domain.ocr import OcrBlock, OcrPage


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ocr-report", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="기존 폴더와 다른 새 평가 폴더")
    args = parser.parse_args()
    source = json.loads(args.ocr_report.read_text(encoding="utf-8"))
    analyzer = GeometryLayoutAnalyzer()
    layouts, summaries = [], []
    for number, recorded in enumerate(source["pages"], 1):
        if recorded["page"] != number:
            raise ValueError("OCR 보고서는 1부터 연속된 페이지 순서여야 합니다.")
        # 과거 OCR 보고서에 이미지 크기가 없으므로 함께 저장된 PNG에서 직접 읽는다.
        # 입력 JSON의 임의 경로를 사용하지 않고 기존 평가 도구의 고정 파일명을 사용한다.
        image = args.ocr_report.parent / "pages" / f"page-{number:05d}.png"
        pixmap = pymupdf.Pixmap(str(image))
        width, height = pixmap.width, pixmap.height
        del pixmap
        page = OcrPage(
            page_id=f"REPLAY-PAGE-{number}",
            ingestion_id="REPLAY",
            pdf_page_number=number,
            width_px=width,
            height_px=height,
            render_dpi=source["dpi"],
            rotation=0,
            blocks=[
                OcrBlock(**block, block_id=f"REPLAY-{number}-{order}", reading_order=order)
                for order, block in enumerate(recorded["blocks"])
            ],
        )
        result = analyzer.analyze(page)
        result.validate_sources(page)
        layouts.append(result.model_dump())
        summaries.append(
            {
                "page": number,
                "kinds": dict(Counter(r.kind for r in result.regions)),
                "needs_review": result.needs_review,
            }
        )
    args.output.mkdir(parents=True, exist_ok=False)
    payload = {
        "source_kind": source.get("source_kind", "unknown"),
        "parser_version": analyzer.version,
        "note": "저장된 OCR 결과의 재분석이며, 분류 정확도나 실제 문서 품질 평가는 아닙니다.",
        "summary": summaries,
        "pages": layouts,
    }
    (args.output / "layout-report.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    # 원문 텍스트는 콘솔에 출력하지 않는다. 보고서도 Block ID/좌표/분류만 포함한다.
    print(json.dumps(summaries, ensure_ascii=False))


if __name__ == "__main__":
    main()
