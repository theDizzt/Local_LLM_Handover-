"""가상 기록관리 업무 데이터로 인수인계서 PDF와 스캔 품질 3단계 PDF를 만든다.

정답 데이터(datasets/<버전>/dataset.json)는 Git에 두고, 생성 PDF는 언제든 다시 만들 수 있으므로
data/synthetic/<버전>/ 아래(Git 제외)에 쓴다. manifest.json은 업무별 PDF 페이지를 기록하여
OCR·검색·문제 생성 결과를 정답 페이지와 비교할 수 있게 한다.
"""

import argparse
import hashlib
import io
import json
from pathlib import Path

import pymupdf

from handover_ai.domain.workforce import HandoverPacket, SyntheticDataset

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = ROOT / "datasets/records-v1/dataset.json"
A4 = pymupdf.paper_rect("a4")
MARGIN = 56
# 내장 CJK 글꼴을 사용해 운영체제 글꼴과 무관하게 같은 PDF를 만든다.
FONT = pymupdf.Font("korea")
TEXT_WIDTH = A4.width - 2 * MARGIN

# docs/plan/develop.md 4.2의 Good/Medium/Poor 기준이다. 값을 바꾸면 manifest에도 기록된다.
SCAN_PROFILES = {
    "good": {"dpi": 300, "rotate": 0.0, "contrast": 1.0, "blur": 0.0, "noise": 0.0, "jpeg": 90},
    "medium": {"dpi": 200, "rotate": 0.8, "contrast": 0.9, "blur": 0.0, "noise": 0.0, "jpeg": 55},
    "poor": {"dpi": 150, "rotate": -2.0, "contrast": 0.75, "blur": 1.0, "noise": 18.0, "jpeg": 40},
}


def load_dataset(path):
    return SyntheticDataset.model_validate_json(Path(path).read_text(encoding="utf-8"))


def wrap(text, size, width):
    lines, current = [], ""
    for word in text.split(" "):
        candidate = f"{current} {word}" if current else word
        if FONT.text_length(candidate, fontsize=size) <= width:
            current = candidate
            continue
        if current:
            lines.append(current)
        current = ""
        # 한 단어가 한 줄보다 길 때만 글자 단위로 자른다.
        for char in word:
            if current and FONT.text_length(current + char, fontsize=size) > width:
                lines.append(current)
                current = ""
            current += char
    if current:
        lines.append(current)
    return lines or [""]


class Writer:
    """줄바꿈과 쪽 넘김만 처리하는 단순 배치기다."""

    def __init__(self, pdf):
        self.pdf = pdf
        self.page = None
        self.y = 0.0

    def new_page(self):
        self.page = self.pdf.new_page(width=A4.width, height=A4.height)
        self.page.insert_font(fontname="ko", fontbuffer=FONT.buffer)
        self.y = MARGIN
        return self.pdf.page_count

    def _line(self, x, text, size):
        leading = size * 1.6
        if self.y + leading > A4.height - MARGIN:
            self.new_page()
        self.page.insert_text((x, self.y + size), text, fontname="ko", fontsize=size)
        self.y += leading

    def text(self, text, size=10.5, indent=0.0, prefix="", gap=3.0):
        offset = FONT.text_length(prefix, fontsize=size) if prefix else 0.0
        for index, line in enumerate(wrap(text, size, TEXT_WIDTH - indent - offset)):
            if index == 0 and prefix:
                self._line(MARGIN + indent, prefix + line, size)
            else:
                self._line(MARGIN + indent + offset, line, size)
        self.y += gap

    def table(self, rows, widths, size=9.0):
        leading, padding = size * 1.5, 4.0
        for row_index, row in enumerate(rows):
            cells = [
                wrap(str(value), size, width - 2 * padding)
                for value, width in zip(row, widths, strict=True)
            ]
            height = max(len(lines) for lines in cells) * leading + 2 * padding
            if self.y + height > A4.height - MARGIN:
                self.new_page()
            x = MARGIN
            for lines, width in zip(cells, widths, strict=True):
                rect = pymupdf.Rect(x, self.y, x + width, self.y + height)
                fill = (0.9, 0.9, 0.9) if row_index == 0 else None
                self.page.draw_rect(rect, color=(0, 0, 0), fill=fill, width=0.6)
                for line_index, line in enumerate(lines):
                    baseline = self.y + padding + line_index * leading + size
                    self.page.insert_text(
                        (x + padding, baseline), line, fontname="ko", fontsize=size
                    )
                x += width
            self.y += height
        self.y += 8


def importance(task):
    # PDF에는 실제 인수인계서처럼 등급만 적고 0~100 원점수는 정답 데이터에만 둔다.
    if task.criticality >= 80:
        return "높음"
    return "보통" if task.criticality >= 60 else "낮음"


def hours(minutes):
    return f"{minutes / 60:g}시간"


def render_handover(dataset: SyntheticDataset, packet: HandoverPacket, path):
    """인수인계서 한 부를 만들고 업무별 PDF 페이지 번호(1부터)를 돌려준다."""
    giver = dataset.employee(packet.giver_id)
    skill_names = {item.skill_id: item.name for item in dataset.skills}
    tasks = dataset.tasks_for(packet.handover_id)
    pages = {}

    with pymupdf.open() as pdf:
        writer = Writer(pdf)
        writer.new_page()
        writer.text("업무인계·인수서", size=20, gap=6)
        writer.text(packet.title, size=13, gap=10)
        writer.table(
            [
                ["항목", "내용"],
                ["기관", dataset.organization],
                ["부서", dataset.department],
                ["인계자", f"{giver.name} ({giver.position})"],
                ["인수자", "평가 후 지정"],
                ["인계일", packet.handover_date.isoformat()],
                ["인계 사유", packet.reason],
            ],
            [110, TEXT_WIDTH - 110],
        )
        writer.text(packet.summary, gap=10)
        writer.text("인계 업무 현황", size=13, gap=4)
        writer.table(
            [["업무 ID", "업무명", "수행 주기", "월 평균 소요"]]
            + [[t.task_id, t.title, t.cycle, hours(t.expected_minutes)] for t in tasks],
            [70, 200, 140, TEXT_WIDTH - 410],
        )

        for task in tasks:
            start = writer.new_page()
            writer.text(f"{task.task_id} {task.title}", size=14, gap=6)
            skills = ", ".join(
                f"{skill_names[skill]} {level}/5" for skill, level in task.required_skills.items()
            )
            for label, value in [
                ("분류", task.category),
                ("중요도", importance(task)),
                ("수행 주기", task.cycle),
                ("월 평균 소요 시간", hours(task.expected_minutes)),
                ("필요 역량", skills),
            ]:
                writer.text(value, prefix=f"{label}: ", gap=1)
            writer.y += 6
            writer.text("1. 업무 개요", size=12)
            writer.text(task.description, indent=12, gap=6)
            writer.text("2. 처리 절차", size=12)
            for order, step in enumerate(task.procedure, start=1):
                writer.text(step, indent=12, prefix=f"{order}) ")
            writer.y += 3
            for title, items in [
                ("3. 주의사항", task.cautions),
                ("4. 문제 발생 시 조치", task.troubleshooting),
            ]:
                writer.text(title, size=12)
                for item in items or ["해당 없음"]:
                    writer.text(item, indent=12, prefix="- ")
                writer.y += 3
            writer.text("5. 관련 근거", size=12)
            references = [
                f"{item.document} {item.pdf_page}쪽" for item in task.manual_references
            ] or ["매뉴얼 근거 없음(기관 내부 기준)"]
            for item in references:
                writer.text(item, indent=12, prefix="- ")
            pages[task.task_id] = list(range(start, pdf.page_count + 1))

        writer.y += 16
        writer.text("인계자: ____________ (서명)        인수자: ____________ (서명)")

        total = pdf.page_count
        for number, page in enumerate(pdf, start=1):
            page.insert_text(
                (A4.width / 2 - 30, A4.height - 28),
                f"{packet.handover_id} · {number}/{total}",
                fontname="ko",
                fontsize=8,
            )
        # 생성 시각과 문서 ID를 고정해 같은 입력이면 같은 해시가 나오게 한다.
        pdf.subset_fonts()
        pdf.set_metadata({"title": packet.title, "creationDate": "", "modDate": ""})
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        pdf.save(path, garbage=3, deflate=True, no_new_id=True)
    return pages


def scan(source_path, target_path, profile_name):
    """텍스트 층이 없는 이미지 PDF로 바꾼다. 잡음 난수는 파일·페이지별로 고정한다."""
    import numpy as np
    from PIL import Image, ImageEnhance, ImageFilter

    profile = SCAN_PROFILES[profile_name]
    with pymupdf.open(source_path) as source, pymupdf.open() as target:
        for index, page in enumerate(source):
            pixmap = page.get_pixmap(dpi=profile["dpi"], colorspace=pymupdf.csGRAY, alpha=False)
            image = Image.frombytes("L", (pixmap.width, pixmap.height), pixmap.samples)
            if profile["contrast"] != 1.0:
                image = ImageEnhance.Contrast(image).enhance(profile["contrast"])
            if profile["rotate"]:
                image = image.rotate(
                    profile["rotate"], resample=Image.Resampling.BICUBIC, fillcolor=255
                )
            if profile["blur"]:
                image = image.filter(ImageFilter.GaussianBlur(profile["blur"]))
            if profile["noise"]:
                key = f"{profile_name}:{Path(source_path).name}:{index}".encode()
                seed = int.from_bytes(hashlib.sha256(key).digest()[:8], "big")
                pixels = np.asarray(image, dtype=np.float32)
                pixels += np.random.default_rng(seed).normal(0, profile["noise"], pixels.shape)
                image = Image.fromarray(np.clip(pixels, 0, 255).astype(np.uint8))
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=profile["jpeg"])
            scanned = target.new_page(width=page.rect.width, height=page.rect.height)
            scanned.insert_image(scanned.rect, stream=buffer.getvalue())
        target.set_metadata({"creationDate": "", "modDate": ""})
        Path(target_path).parent.mkdir(parents=True, exist_ok=True)
        target.save(target_path, garbage=3, deflate=True, no_new_id=True)


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def build(dataset_path, output, profiles):
    dataset = load_dataset(dataset_path)
    output = Path(output)
    files, tasks = [], {}

    def record(quality, handover_id, path):
        with pymupdf.open(path) as pdf:
            page_count = pdf.page_count
        files.append(
            {
                "quality": quality,
                "handover_id": handover_id,
                "path": path.relative_to(output).as_posix(),
                "pages": page_count,
                "sha256": sha256(path),
            }
        )

    for packet in dataset.handovers:
        clean = output / "clean" / f"{packet.handover_id}.pdf"
        for task_id, pages in render_handover(dataset, packet, clean).items():
            tasks[task_id] = {"handover_id": packet.handover_id, "pdf_pages": pages}
        record("clean", packet.handover_id, clean)
        # 스캔본은 정상 PDF를 페이지 단위로 그대로 옮기므로 업무별 페이지 번호가 같다.
        for name in profiles:
            scanned = output / "scans" / name / f"{packet.handover_id}.pdf"
            scan(clean, scanned, name)
            record(name, packet.handover_id, scanned)

    manifest = {
        "dataset_version": dataset.dataset_version,
        "dataset_sha256": sha256(dataset_path),
        "review_status": dataset.review_status,
        "scan_profiles": {name: SCAN_PROFILES[name] for name in profiles},
        "files": files,
        "tasks": tasks,
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--output", type=Path, help="기본값: data/synthetic/<dataset_version>")
    parser.add_argument(
        "--profiles", nargs="*", choices=list(SCAN_PROFILES), default=list(SCAN_PROFILES)
    )
    args = parser.parse_args()
    output = args.output or ROOT / "data/synthetic" / load_dataset(args.dataset).dataset_version
    manifest = build(args.dataset, output, args.profiles)
    print(f"{len(manifest['files'])}개 PDF, 업무 {len(manifest['tasks'])}개 → {output}")


if __name__ == "__main__":
    main()
