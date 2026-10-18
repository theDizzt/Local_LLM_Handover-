"""[읽기 안내] 모델이 고른 OCR 줄을 같은 페이지 안에서 문장 단위로 잇는 순수 규칙이다.

OCR은 문장을 줄마다 나눠 Block으로 저장한다. 모델이 문장의 한 줄만 고르면 답의 핵심이
빠지므로, 저장된 Block만 써서 앞뒤 줄을 덧붙인다. 문장이 Chunk 경계에서 나뉘는 경우가 많아
Chunk가 아니라 구조 버전의 페이지 읽기 순서를 기준으로 한다. 모델 입력·프롬프트는 바꾸지 않는다.
2단 문서에서 옆 단의 줄을 붙이지 않도록 좌표로 같은 단·인접 줄인지 확인한다.
"""

import re

from handover_ai.domain.source_guard import exclusion_reason

VERSION = "sentence-join-v1"
MAX_EXTRA_LINES = 3
MAX_CHARS = 400

# 한국어 공문서 문장 끝(…한다, …함, …임 등)과 문장부호
_SENTENCE_END = re.compile(r"(?:[다요음함임됨것]|[.?!。:;])\s*$")
# 목록·번호·제목처럼 새 항목을 시작하는 줄. OCR이 가운뎃점을 '.'으로 읽는 경우도 포함한다.
_ITEM_START = re.compile(
    r"^\s*(?:[-–·•*ㅇ○●◦□■▶※.]|\(?\d+(?:\.\d+)*[.)]|[①-⑳]|[가-하][.)]|[A-Z][.)]|제\d+)"
)
_CIRCLED = re.compile(r"^\s*[①-⑳]")


def _ends_sentence(text):
    return bool(_SENTENCE_END.search(text))


def _starts_item(text):
    return bool(_ITEM_START.match(text))


def _same_column(upper, lower):
    """lower가 upper 바로 아래, 같은 단에 있는 줄인지 정규화 좌표로 판단한다."""
    ax0, ay0, ax1, ay1 = upper["bbox"]
    bx0, by0, bx1, by1 = lower["bbox"]
    overlap = min(ax1, bx1) - max(ax0, bx0)
    narrower = min(ax1 - ax0, bx1 - bx0)
    aligned = overlap >= 0.5 * narrower or abs(ax0 - bx0) < 0.03
    height = max(ay1 - ay0, by1 - by0)
    gap = by0 - ay1
    return aligned and -0.5 * height <= gap <= 1.5 * height


def _joinable(upper, lower):
    # ①②… 목록에서 OCR이 ⑥을 '6'으로 읽으면 번호 판별을 놓치므로 이어 붙이지 않는다.
    if _CIRCLED.match(upper["text"]) and lower["text"].lstrip()[:1].isdigit():
        return False
    # 번호로 시작하면서 아래 본문보다 짧은 줄은 제목으로 보고 본문에 붙이지 않는다.
    # 번호 문단의 첫 줄(예: "7.이 가이드에서 정한 …")은 본문 폭이라 그대로 잇는다.
    width = upper["bbox"][2] - upper["bbox"][0]
    if _starts_item(upper["text"]) and width < 0.75 * (lower["bbox"][2] - lower["bbox"][0]):
        return False
    return (
        not _ends_sentence(upper["text"])
        and not _starts_item(lower["text"])
        and _same_column(upper, lower)
    )


def _below(block, blocks):
    lower = [c for c in blocks if c is not block and _same_column(block, c)]
    return min(lower, key=lambda c: c["bbox"][1], default=None)


def _above(block, blocks):
    upper = [c for c in blocks if c is not block and _same_column(c, block)]
    return max(upper, key=lambda c: c["bbox"][3], default=None)


def join_sentences(items, page_blocks):
    """선택 항목을 문장 단위 발췌로 바꾼다.

    items: materialize 결과(모델 선택 순서). page_blocks: page_id → 해당 페이지의 Block 목록.
    같은 페이지·같은 분류에서 위아래로 이웃한 선택 줄은 한 항목으로 합치고, 문장이 끝나지
    않았으면 앞뒤 줄을 최대 MAX_EXTRA_LINES개씩 덧붙인다. 이웃 줄은 저장된 읽기 순서가 아니라
    좌표로 찾는다. 2단 페이지에서 읽기 순서가 섞여도 같은 단의 바로 위아래 줄만 잇기 위해서다.
    다른 항목의 줄과 지시 문형 줄은 붙이지 않는다.
    """
    page_of = {
        block["block_id"]: (page_id, block)
        for page_id, blocks in page_blocks.items()
        for block in blocks
    }
    selected = {item["block"]["block_id"] for item in items}
    groups = []
    for item in items:
        found = page_of.get(item["block"]["block_id"])
        if found is None:
            groups.append({"item": item, "page": None, "lines": []})
            continue
        page_id, block = found
        blocks = page_blocks[page_id]
        for group in groups:
            if group["page"] != page_id or group["item"]["category"] != item["category"]:
                continue
            if _below(group["lines"][-1], blocks) is block:
                group["lines"].append(block)
                break
            if _above(group["lines"][0], blocks) is block:
                group["lines"].insert(0, block)
                break
        else:
            groups.append({"item": item, "page": page_id, "lines": [block]})

    used = {line["block_id"] for group in groups for line in group["lines"]} | selected

    def usable(block):
        return (
            block is not None
            and block["block_id"] not in used
            and block["text"].strip()
            and not exclusion_reason(block["text"])
        )

    results = []
    for group in groups:
        item, lines = group["item"], group["lines"]
        if group["page"] is None:
            results.append(item)
            continue
        blocks = page_blocks[group["page"]]
        chosen = {line["block_id"] for line in lines}
        length = sum(len(line["text"]) for line in lines)
        for _ in range(MAX_EXTRA_LINES):
            before = _above(lines[0], blocks)
            if not usable(before) or not _joinable(before, lines[0]):
                break
            if length + len(before["text"]) > MAX_CHARS:
                break
            lines.insert(0, before)
            length += len(before["text"])
            used.add(before["block_id"])
        for _ in range(MAX_EXTRA_LINES):
            after = _below(lines[-1], blocks)
            if not usable(after) or not _joinable(lines[-1], after):
                break
            if length + len(after["text"]) > MAX_CHARS:
                break
            lines.append(after)
            length += len(after["text"])
            used.add(after["block_id"])
        results.append(
            {
                **item,
                "excerpt": {
                    "version": VERSION,
                    "text": " ".join(line["text"].strip() for line in lines),
                    "blocks": lines,
                    "selected_block_ids": [b["block_id"] for b in lines if b["block_id"] in chosen],
                    "added_block_ids": [
                        b["block_id"] for b in lines if b["block_id"] not in chosen
                    ],
                },
            }
        )
    return results
