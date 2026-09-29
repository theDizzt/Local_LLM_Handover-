from pathlib import Path

import pymupdf

from handover_ai.domain.documents import InvalidDocument


class PyMuPdfInspector:
    def page_count(self, path: Path) -> int:
        # 확장자/MIME은 사용자가 바꿀 수 있다. 실제 파서로 열어 PDF인지 확인한다.
        # Windows에서는 손상 파일을 경로로 열 때 파서 예외의 traceback이 파일
        # 핸들을 붙잡아 삭제를 방해할 수 있다. 크기 검사를 통과한 파일만 bytes로
        # 읽어 전달하여 파서가 원본 파일 핸들을 소유하지 않도록 한다.
        # 이 검사 단계에는 파일 크기만큼의 메모리와 파서의 추가 메모리가 필요하다.
        content = path.read_bytes()
        try:
            with pymupdf.open(stream=content, filetype="pdf") as pdf:
                if not pdf.is_pdf:
                    raise InvalidDocument("PDF 파일만 등록할 수 있습니다.")
                if pdf.needs_pass:
                    raise InvalidDocument("암호를 해제한 PDF를 등록해 주세요.")
                if pdf.page_count < 1:
                    raise InvalidDocument("페이지가 없는 PDF는 등록할 수 없습니다.")
                return pdf.page_count
        except (pymupdf.FileDataError, pymupdf.EmptyFileError) as exc:
            raise InvalidDocument("PDF 파일이 비어 있거나 손상되었습니다.") from exc
