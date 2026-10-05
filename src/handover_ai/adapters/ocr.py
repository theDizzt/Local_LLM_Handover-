# [읽기 안내] 외부 라이브러리의 결과를 우리 도메인 모델로 바꾸는 어댑터다.
# PyMuPDF는 페이지를 PNG로 저장하고 PaddleOCR는 그 PNG의 글자와 위치를 읽는다.
# PaddleOCR는 선택 의존성이므로 서버 import 시점이 아닌 첫 추론 시점에 불러온다.
from collections.abc import Iterator
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from threading import Lock

import pymupdf

from handover_ai.domain.ocr import OcrLine, OcrUnavailable, RenderedPage

# 이 모듈의 페이지 렌더링과 Paddle 추론을 각각 직렬화한다.
# 이 잠금은 프로세스 내부에서만 유효하며 분산 worker 사이의 잠금은 아니다.
_render_lock = Lock()
_inference_lock = Lock()


class PyMuPdfRenderer:
    def __init__(self, dpi: int = 150, max_pixels: int = 25_000_000):
        if not 72 <= dpi <= 300 or max_pixels < 1:
            raise ValueError("렌더링 DPI는 72~300, 최대 픽셀 수는 양수여야 합니다.")
        self.dpi = dpi
        self.max_pixels = max_pixels
        self.version = f"pymupdf-{pymupdf.VersionBind}:dpi={dpi}"

    def render(self, source: Path, destination: Path) -> Iterator[RenderedPage]:
        destination.mkdir(parents=True, exist_ok=True)
        # PDF를 페이지마다 열면 느리지만 yield 사이에 네이티브 객체가 공유되지 않고
        # 한 페이지만 메모리에 존재한다. 다음 단계에서 별도 프로세스 worker로 대체 가능하다.
        with _render_lock, pymupdf.open(source) as pdf:
            count = pdf.page_count
        for index in range(count):
            with _render_lock, pymupdf.open(source) as pdf:
                page = pdf[index]
                rect = page.rect * (self.dpi / 72)
                # PDF 좌표는 1/72인치 단위다. 목표 DPI/72를 곱해 예상 픽셀 수를 구한다.
                # 이미지를 만든 뒤 크기를 검사하면 늦으므로 생성 전에 상한을 확인한다.
                if (int(rect.width) + 1) * (int(rect.height) + 1) > self.max_pixels:
                    raise ValueError("페이지 렌더링 픽셀 제한을 초과했습니다.")
                pixmap = page.get_pixmap(dpi=self.dpi, colorspace=pymupdf.csRGB, alpha=False)
                image = destination / f"page-{index + 1:05d}.png"
                pixmap.save(image)
                result = RenderedPage(
                    number=index + 1,
                    image_path=image,
                    width=pixmap.width,
                    height=pixmap.height,
                    dpi=self.dpi,
                    rotation=page.rotation,
                )
                del pixmap
            yield result


class PaddleOcrEngine:
    def __init__(
        self,
        language: str = "korean",
        detection_dir: str | None = None,
        recognition_dir: str | None = None,
    ):
        # 모델 이름을 지정하면 PaddleOCR는 lang 옵션을 무시하므로 인식 모델도
        # 언어별로 명시한다. 지원하지 않는 언어를 묵시적으로 다른 모델에 보내지 않는다.
        recognition_models = {
            "korean": "korean_PP-OCRv5_mobile_rec",
            "en": "en_PP-OCRv5_mobile_rec",
        }
        if language not in recognition_models:
            raise ValueError("OCR_LANGUAGE는 korean 또는 en이어야 합니다.")
        self.language = language
        self.recognition_model = recognition_models[language]
        self.detection_dir = detection_dir
        self.recognition_dir = recognition_dir
        self._engine = None

    @property
    def version(self) -> str:
        # 패키지 메타데이터만 읽으므로 이 조회만으로 모델을 로딩/다운로드하지 않는다.
        try:
            return f"paddleocr-{version('paddleocr')}:PP-OCRv5-mobile-det:{self.language}"
        except PackageNotFoundError as exc:
            raise OcrUnavailable(
                "OCR 선택 의존성을 설치해 주세요: pip install -e '.[ocr]'"
            ) from exc

    def recognize(self, page: RenderedPage) -> list[OcrLine]:
        with _inference_lock:
            if self._engine is None:
                try:
                    from paddleocr import PaddleOCR

                    options = {}
                    if self.detection_dir:
                        options["text_detection_model_dir"] = self.detection_dir
                    if self.recognition_dir:
                        options["text_recognition_model_dir"] = self.recognition_dir
                    # 자동 문서 회전/왜곡 보정은 결과 좌표계를 바꿀 수 있으므로 끈다.
                    # bbox는 반드시 저장된 PNG에 직접 겹쳐 그릴 수 있어야 한다.
                    self._engine = PaddleOCR(
                        # 로컬 CPU에서 기본 server 검출 모델의 지연이 커 경량 모델을 쓴다.
                        # 사용자 지정 모델 폴더도 이 검출 모델과 호환되는 것을 지정한다.
                        text_detection_model_name="PP-OCRv5_mobile_det",
                        text_recognition_model_name=self.recognition_model,
                        device="cpu",
                        cpu_threads=2,
                        # Windows CPU의 oneDNN/PIR 연산 호환 오류를 피하도록
                        # MKL-DNN 최적화를 끈다. 속도보다 기본 실행 호환성을 우선한다.
                        enable_mkldnn=False,
                        use_doc_orientation_classify=False,
                        use_doc_unwarping=False,
                        use_textline_orientation=False,
                        **options,
                    )
                except Exception as exc:
                    raise OcrUnavailable("OCR 런타임 또는 모델을 초기화하지 못했습니다.") from exc
            results = list(self._engine.predict(str(page.image_path)))
        if len(results) != 1:
            raise ValueError("한 페이지에 대한 OCR 결과가 필요합니다.")
        result = results[0]
        lines = []
        # PaddleOCR 3.x의 rec_polys는 rec_texts/rec_scores와 같은 순서다.
        # strict=True로 길이가 다르면 실패시켜 잘못된 근거 연결을 방지한다.
        for text, score, polygon in zip(
            result["rec_texts"], result["rec_scores"], result["rec_polys"], strict=True
        ):
            if not text.strip():
                continue
            xs = [float(point[0]) / page.width for point in polygon]
            # Paddle의 다각형 꼭짓점을 감싸는 직사각형으로 바꾸고 0~1로 정규화한다.
            # 소수점 오차로 가장자리를 벗어난 값은 자르며, 역전/면적 0은 모델이 거부한다.
            ys = [float(point[1]) / page.height for point in polygon]
            lines.append(
                OcrLine(
                    text=text,
                    confidence=float(score),
                    bbox=(max(0, min(xs)), max(0, min(ys)), min(1, max(xs)), min(1, max(ys))),
                )
            )
        return lines
