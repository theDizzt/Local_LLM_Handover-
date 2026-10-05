"""저장된 OCR 사각형을 이용하는 보수적인 배치 분석기.

이 분석기는 선/이미지/폰트나 표 셀을 보는 모델이 아니다. 정렬된 짧은 3열 이상은
표 후보, 명확한 세로 여백이 있는 긴 본문은 2단 후보로 제안한다. 짧은 2열은 표와
다단 구분이 불가능하므로 기존 OCR 순서를 유지한다. 모든 결과는 사람이 검토해야 한다.
"""

from statistics import median

from handover_ai.domain.layout import LAYOUT_VERSION, LayoutRegion, PageLayout, enclosing_box
from handover_ai.domain.ocr import OcrBlock, OcrPage


def region(
    kind: str, blocks: list[OcrBlock], reason: str | None = None, column: int | None = None
) -> LayoutRegion:
    return LayoutRegion(
        kind=kind,
        block_ids=[b.block_id for b in blocks],
        bbox=enclosing_box(blocks),
        column=column,
        review_reasons=[reason] if reason else [],
    )


class GeometryLayoutAnalyzer:
    version = LAYOUT_VERSION

    def analyze(self, page: OcrPage) -> PageLayout:
        blocks = [b for b in page.blocks if b.text.strip()]
        result = PageLayout(
            page_id=page.page_id,
            ingestion_id=page.ingestion_id,
            pdf_page_number=page.pdf_page_number,
            regions=[],
        )
        if not blocks:
            result.warnings = ["empty_page"]
            return result
        # 신뢰도처럼 보이는 임의 확률을 부여하지 않는다. 규칙 기반 제안이라는 경고를
        # 항상 반환하며, 낮은 OCR 신뢰도와 분석 불확실성을 별도로 표시한다.
        result.warnings = ["heuristic_layout"]
        if any(b.confidence < 0.8 for b in blocks):
            result.warnings.append("low_ocr_confidence")
        ordered = sorted(blocks, key=lambda b: (b.bbox[1], b.bbox[0], b.reading_order))
        # 매우 복잡한 페이지에서 조합 탐색을 제한한다. 제한 초과도 누락 없이 원문 유지한다.
        if len(blocks) > 1500 or self._overlaps(ordered):
            reason = "complex_page" if len(blocks) > 1500 else "overlapping_boxes"
            result.regions = [
                region("unknown", sorted(blocks, key=lambda b: b.reading_order), reason)
            ]
            result.validate_sources(page)
            return result
        height = median(b.bbox[3] - b.bbox[1] for b in blocks)
        rows = self._rows(ordered)
        pending: list[OcrBlock] = []

        def flush():
            if pending:
                result.regions.extend(self._body(pending))
                pending.clear()

        index = 0
        while index < len(rows):
            row = rows[index]
            first = row[0]
            # 글자 높이가 확실히 크고 짧은 독립 행만 제목 후보로 취급한다.
            # 번호가 있다는 이유만으로 절차 문장을 제목으로 바꾸지 않는다.
            heading = (
                len(row) == 1
                and len(first.text) <= 80
                and first.bbox[3] - first.bbox[1] >= height * 1.5
            )
            if heading:
                flush()
                result.regions.append(region("heading", row, "heading_candidate"))
                index += 1
                continue
            band = self._table_band(rows, index, height)
            if len(band) >= 3:
                flush()
                members = [block for item in band for block in item]
                if len(row) >= 3:
                    result.regions.append(
                        region("table_candidate", members, "table_cells_unverified")
                    )
                else:
                    # 짧은 2열은 표/목록/다단이 모두 가능하다. 순서를 바꾸는 대신 검토로 보낸다.
                    result.regions.append(
                        region(
                            "unknown",
                            sorted(members, key=lambda b: b.reading_order),
                            "table_or_columns",
                        )
                    )
                index += len(band)
                continue
            if len(row) == 1 and first.bbox[2] - first.bbox[0] > 0.7:
                # 양쪽 열을 가로지르는 본문은 세로 구간을 나누는 경계다.
                # 이 행을 옆 열에 잘못 합치지 않도록 앞/뒤 구간을 독립 분석한다.
                flush()
                result.regions.append(region("paragraph", row))
            else:
                pending.extend(row)
            index += 1
        flush()
        result.validate_sources(page)
        return result

    @staticmethod
    def _overlaps(blocks: list[OcrBlock]) -> bool:
        for index, left in enumerate(blocks):
            x0, y0, x1, y1 = left.bbox
            for right in blocks[index + 1 :]:
                a0, b0, a1, b1 = right.bbox
                if b0 >= y1:
                    break
                intersection = max(0, min(x1, a1) - max(x0, a0)) * max(0, min(y1, b1) - max(y0, b0))
                if intersection > 0.3 * min((x1 - x0) * (y1 - y0), (a1 - a0) * (b1 - b0)):
                    return True
        return False

    @staticmethod
    def _rows(blocks: list[OcrBlock]) -> list[list[OcrBlock]]:
        rows: list[list[OcrBlock]] = []
        for block in blocks:
            if rows:
                anchor = rows[-1][0]
                overlap = min(anchor.bbox[3], block.bbox[3]) - max(anchor.bbox[1], block.bbox[1])
                if overlap >= 0.5 * min(
                    anchor.bbox[3] - anchor.bbox[1], block.bbox[3] - block.bbox[1]
                ):
                    rows[-1].append(block)
                    continue
            rows.append([block])
        return [sorted(row, key=lambda b: b.bbox[0]) for row in rows]

    @staticmethod
    def _table_band(rows: list[list[OcrBlock]], start: int, height: float) -> list[list[OcrBlock]]:
        reference = rows[start]
        if len(reference) < 2:
            return []
        band = []
        for row in rows[start:]:
            if len(row) != len(reference) or any(len(b.text) > 24 for b in row):
                break
            if any(abs(a.bbox[0] - b.bbox[0]) > 0.025 for a, b in zip(reference, row, strict=True)):
                break
            if band and row[0].bbox[1] - band[-1][0].bbox[3] > height * 3:
                break
            band.append(row)
        return band

    @staticmethod
    def _body(blocks: list[OcrBlock]) -> list[LayoutRegion]:
        # 모든 행을 가로지르지 않는 수직 여백만 gutter로 인정한다. 두 열 모두 최소 두 행이
        # 있어야 하며, 세로 범위가 겹쳐야 들여쓰기/앞뒤 문단을 다단으로 오해하지 않는다.
        edges = sorted({b.bbox[0] for b in blocks} | {b.bbox[2] for b in blocks})
        candidates = []
        for a, b in zip(edges, edges[1:], strict=False):
            middle = (a + b) / 2
            if b - a < 0.04 or not 0.2 < middle < 0.8:
                continue
            left = [item for item in blocks if item.bbox[2] <= middle]
            right = [item for item in blocks if item.bbox[0] >= middle]
            if min(len(left), len(right)) < 2 or len(left) + len(right) != len(blocks):
                continue
            lbox, rbox = enclosing_box(left), enclosing_box(right)
            if min(lbox[3], rbox[3]) <= max(lbox[1], rbox[1]):
                continue
            candidates.append((left, right))
        if len(candidates) == 1:
            # 두 행뿐인 짧은 2열도 표/목록과 구별할 근거가 부족하다.
            # 위의 3행 이상 표 후보 규칙에 걸리지 않았다고 본문 다단으로 확정하지 않는다.
            if all(len(block.text) <= 24 for block in blocks):
                return [
                    region(
                        "unknown", sorted(blocks, key=lambda b: b.reading_order), "table_or_columns"
                    )
                ]
            return [
                region(
                    "paragraph",
                    sorted(column, key=lambda item: (item.bbox[1], item.bbox[0])),
                    "column_order_candidate",
                    index + 1,
                )
                for index, column in enumerate(candidates[0])
            ]
        rows = GeometryLayoutAnalyzer._rows(sorted(blocks, key=lambda b: (b.bbox[1], b.bbox[0])))
        if any(len(row) > 1 for row in rows):
            return [
                region(
                    "unknown",
                    sorted(blocks, key=lambda b: b.reading_order),
                    "ambiguous_reading_order",
                )
            ]
        return [region("paragraph", [row[0] for row in rows])]
