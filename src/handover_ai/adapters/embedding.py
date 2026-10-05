"""로컬 E5 모델. 네트워크 다운로드는 별도 준비 스크립트에서만 허용한다."""

import hashlib
from functools import cached_property
from importlib.metadata import version
from pathlib import Path
from threading import Lock

from handover_ai.domain.search import SearchUnavailable, validate_vectors


class LocalE5Embedder:
    def __init__(self, path: Path):
        self.path = path
        self.lock = Lock()

    @cached_property
    def version(self) -> str:
        if not (self.path / "model.safetensors").is_file():
            raise SearchUnavailable("먼저 scripts/prepare_embedding_model.py를 실행해 주세요.")
        # 파일 내용과 전처리 정책 모두 색인 버전에 포함한다. 모델 교체 후에는 서버를
        # 재시작한다. 경로 이름만 비교하면 같은 경로의 다른 가중치를 구분할 수 없다.
        digest = hashlib.sha256(b"e5-prefix:windows-mean-v1:512")
        try:
            for package in ("sentence-transformers", "transformers", "torch"):
                digest.update(f"{package}:{version(package)}".encode())
        except Exception as exc:
            raise SearchUnavailable("검색 의존성 [rag]를 설치해 주세요.") from exc
        for path in sorted(self.path.rglob("*")):
            if path.is_file() and ".cache" not in path.parts:
                digest.update(path.relative_to(self.path).as_posix().encode())
                with path.open("rb") as stream:
                    for block in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(block)
        return digest.hexdigest()

    @cached_property
    def model(self):
        _ = self.version
        try:
            from sentence_transformers import SentenceTransformer

            model = SentenceTransformer(
                str(self.path.resolve()),
                device="cpu",
                local_files_only=True,
                trust_remote_code=False,
            )
            model.max_seq_length = min(model.max_seq_length, 512)
            return model
        except Exception as exc:
            raise SearchUnavailable("로컬 임베딩 모델을 읽을 수 없습니다.") from exc

    def windows(self, text: str, prefix: str) -> list[str]:
        # 긴 OCR 줄도 끝부분을 버리지 않는다. 실제 토크나이저 길이를 기준으로 문자를
        # 나누고 모든 창을 평균한다. 원문과 근거 좌표는 SQLite에 그대로 보존된다.
        result = []
        while text:
            low, high = 1, len(text)
            while low < high:
                middle = (low + high + 1) // 2
                size = len(self.model.tokenizer.encode(prefix + text[:middle], verbose=False))
                if size <= self.model.max_seq_length:
                    low = middle
                else:
                    high = middle - 1
            result.append(prefix + text[:low])
            text = text[low:]
        return result

    def encode(self, texts: list[str], *, query: bool = False) -> list[list[float]]:
        import numpy as np

        result = []
        with self.lock:
            for text in texts:
                windows = self.windows(text, "query: " if query else "passage: ")
                vectors = self.model.encode(windows, normalize_embeddings=True)
                vector = np.mean(vectors, axis=0)
                result.append((vector / np.linalg.norm(vector)).tolist())
        validate_vectors(result, len(texts))
        return result
