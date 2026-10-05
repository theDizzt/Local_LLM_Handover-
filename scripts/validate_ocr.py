"""한국어 합성 스캔 또는 실제 PDF의 OCR 결과와 정답 대비 오류율을 기록한다.

실행 예시는 docs/ocr-review.md 참고. 출력은 Git에서 제외된 data에 두는 것을 권장한다.
이 도구는 앱 DB를 변경하지 않으며 별도 폴더에 렌더링 이미지와 평가 JSON을 저장한다.
"""

import argparse
import io
import json
import unicodedata
from pathlib import Path
from time import perf_counter

import pymupdf

from handover_ai.adapters.ocr import PaddleOcrEngine, PyMuPdfRenderer
from handover_ai.config import load_settings


def normalized(text: str) -> str:
    # 한글 조합 방식과 OCR의 공백/줄바꿈 차이를 제외한 문자 오류율이다.
    # 공백 제거 때문에 띄어쓰기 품질은 평가하지 않는다. 숫자·문장부호는 유지한다.
    return "".join(unicodedata.normalize("NFC", text).split())


def character_error_rate(reference: str, actual: str) -> float | None:
    expected, found = normalized(reference), normalized(actual)
    if not expected:
        return None
    # Levenshtein 편집 거리: 누락/추가/대체를 각각 1회 오류로 센다.
    # 이전 행만 유지하여 긴 페이지에서도 O(인식 문자 수) 메모리를 사용한다.
    # 추가 인식이 많으면 CER은 1보다 클 수 있으므로 100%에서 잘라 버리지 않는다.
    previous = list(range(len(found) + 1))
    for i, left in enumerate(expected, 1):
        current = [i]
        for j, right in enumerate(found, 1):
            current.append(min(current[-1] + 1, previous[j] + 1, previous[j - 1] + (left != right)))
        previous = current
    return previous[-1] / len(expected)


def make_fixture(destination: Path, font: Path) -> tuple[Path, list[str]]:
    from PIL import Image, ImageEnhance, ImageFilter

    if not font.is_file():
        raise ValueError("합성 문서 생성에 필요한 한글 글꼴을 --font로 지정해 주세요.")
    body = [
        "업무 인수인계 점검표",
        "담당자: 김민수",
        "매일 오전 09:30 서버 상태를 확인한다.",
        "오류 발생 시 02-1234-5678로 연락한다.",
        "백업 보관 기간은 30일이며 매주 금요일 점검한다.",
    ]
    table = [
        ["점검 항목", "주기", "기준"],
        ["서버 점검", "매일", "09:30"],
        ["백업 확인", "매주", "30일"],
        ["용량 확인", "매월", "80% 이하"],
    ]
    references = ["\n".join(body), "\n".join(" ".join(row) for row in table), "\n".join(body)]
    images = []
    with pymupdf.open() as source:
        for is_table in (False, True):
            page = source.new_page(width=600, height=350)
            page.insert_font(fontname="Korean", fontfile=str(font))
            if is_table:
                for row_index, row in enumerate(table):
                    for column_index, value in enumerate(row):
                        x, y = 30 + column_index * 180, 30 + row_index * 65
                        page.draw_rect(pymupdf.Rect(x, y, x + 180, y + 65), color=(0.4, 0.4, 0.4))
                        page.insert_text((x + 12, y + 40), value, fontname="Korean", fontsize=17)
            else:
                for index, line in enumerate(body):
                    page.insert_text((30, 60 + index * 50), line, fontname="Korean", fontsize=19)
            images.append(Image.open(io.BytesIO(page.get_pixmap(dpi=150).tobytes("png"))))
    images.append(
        ImageEnhance.Contrast(images[0].filter(ImageFilter.GaussianBlur(2))).enhance(0.35)
    )
    path = destination / "synthetic-korean.pdf"
    # 이미지로 다시 만든 PDF에는 검색 가능한 텍스트가 없다. 엔진이 실제 픽셀을 읽어야 한다.
    with pymupdf.open() as pdf:
        for image in images:
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            page = pdf.new_page(width=600, height=350)
            page.insert_image(page.rect, stream=buffer.getvalue())
        pdf.save(path)
    return path, references


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--synthetic", action="store_true")
    source.add_argument("--pdf", type=Path)
    parser.add_argument(
        "--reference", type=Path, help="페이지 순서대로 정답 문자열을 담은 JSON 배열"
    )
    parser.add_argument("--font", type=Path, default=Path("C:/Windows/Fonts/malgun.ttf"))
    parser.add_argument(
        "--output", type=Path, required=True, help="새 평가 폴더; 기존 폴더는 덮어쓰지 않음"
    )
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    references = None
    if args.synthetic:
        path, references = make_fixture(args.output, args.font)
        (args.output / "reference.json").write_text(
            json.dumps(references, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    else:
        path = args.pdf
        if args.reference:
            references = json.loads(args.reference.read_text(encoding="utf-8"))
    with pymupdf.open(path) as pdf:
        if references is not None and (
            not isinstance(references, list)
            or len(references) != pdf.page_count
            or not all(isinstance(item, str) for item in references)
        ):
            raise ValueError("정답은 PDF 페이지 수와 같은 길이의 문자열 배열이어야 합니다.")
    settings = load_settings()
    engine = PaddleOcrEngine(
        settings.ocr_language, settings.ocr_detection_model_dir, settings.ocr_recognition_model_dir
    )
    report = {
        "source_kind": "synthetic" if args.synthetic else "user_pdf",
        "engine": engine.version,
        "dpi": settings.ocr_dpi,
        "cer_policy": "NFC, whitespace removed; punctuation and digits preserved",
        "note": "신뢰도는 정확도가 아닙니다. 첫 페이지 추론 시간은 모델 초기화를 포함합니다.",
        "pages": [],
    }
    started = perf_counter()
    for page in PyMuPdfRenderer(settings.ocr_dpi, settings.ocr_max_pixels).render(
        path, args.output / "pages"
    ):
        before = perf_counter()
        lines = engine.recognize(page)
        actual = "\n".join(line.text for line in lines)
        report["pages"].append(
            {
                "page": page.number,
                "ocr_seconds": round(perf_counter() - before, 3),
                "text": actual,
                "blocks": [line.model_dump() for line in lines],
                "cer": character_error_rate(references[page.number - 1], actual)
                if references is not None
                else None,
            }
        )
    report["total_seconds"] = round(perf_counter() - started, 3)
    (args.output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    # OCR 원문은 콘솔에 출력하지 않는다. 업무 자료 평가 시 문서 내용은 결과 파일에만 둔다.
    print(
        json.dumps(
            {
                "report": str(args.output / "report.json"),
                "pages": [
                    {
                        key: value
                        for key, value in page.items()
                        if key in ("page", "ocr_seconds", "cer")
                    }
                    for page in report["pages"]
                ],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
