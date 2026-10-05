# [읽기 안내] 모델을 실제로 실행하는 선택 통합 테스트다. 기본 테스트는 다운로드 없이
# 실행되도록 건너뛴다. 실행 방법과 모델 캐시 설정은 docs/ocr-processing.md에 설명한다.
import io
import os

import pymupdf
import pytest

from handover_ai.adapters.ocr import PaddleOcrEngine, PyMuPdfRenderer


@pytest.mark.skipif(os.getenv("RUN_OCR_LIVE") != "1", reason="실제 OCR 모델 선택 테스트")
def test_real_ocr_scan_quality(tmp_path):
    from PIL import Image, ImageEnhance, ImageFilter

    # 개인정보 없는 동일 문장을 세 가지 스캔 품질로 만들어 결과 형식과 좋은 스캔의
    # 핵심 단어를 확인한다. 이것만으로 실제 한국어 업무 문서 정확도를 보장하지 않는다.
    with pymupdf.open() as original:
        page = original.new_page(width=480, height=180)
        page.insert_text((30, 80), "Check DTC before restart", fontsize=24)
        png = page.get_pixmap(dpi=150).tobytes("png")
    base = Image.open(io.BytesIO(png)).convert("RGB")
    medium = ImageEnhance.Contrast(base.filter(ImageFilter.GaussianBlur(0.8))).enhance(0.65)
    poor = ImageEnhance.Contrast(base.filter(ImageFilter.GaussianBlur(2))).enhance(0.25)
    engine = PaddleOcrEngine()
    results = {}
    for quality, image in [("good", base), ("medium", medium), ("poor", poor)]:
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        source = tmp_path / f"{quality}.pdf"
        with pymupdf.open() as pdf:
            page = pdf.new_page(width=480, height=180)
            page.insert_image(page.rect, stream=buffer.getvalue())
            pdf.save(source)
        rendered = next(PyMuPdfRenderer(150).render(source, tmp_path / quality))
        lines = engine.recognize(rendered)
        results[quality] = " ".join(line.text for line in lines)
        assert all(0 <= line.confidence <= 1 for line in lines)
    assert "DTC" in results["good"].upper()
    print("OCR quality fixture results:", results)
