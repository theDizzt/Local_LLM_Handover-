"""로컬 safetensors 모델을 별도 프로세스에서 실행하여 시간 초과 시 GPU 작업도 종료한다."""

import asyncio
import json
import sys
from pathlib import Path

from handover_ai.domain.drafts import DraftFailure, DraftSelection
from handover_ai.domain.search import SearchUnavailable


def _kill_descendants(pid):
    import psutil

    try:
        children = psutil.Process(pid).children(recursive=True)
    except psutil.NoSuchProcess:
        return
    for child in reversed(children):
        try:
            child.kill()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(children, timeout=5)
    if alive:
        raise RuntimeError("model worker termination failed")


async def terminate_worker(process):
    try:
        # Windows venv 실행기 아래의 실제 인터프리터부터 회수한다.
        if sys.platform == "win32" and process.returncode is None:
            await asyncio.to_thread(_kill_descendants, process.pid)
    finally:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        await process.wait()


class LocalQwenDraftGateway:
    provider = "transformers"
    prompt_version = "qwen-excerpt-v9-source-guard"

    def __init__(self, path: Path, timeout: int, adapter: Path | None = None):
        self.path, self.timeout = path.resolve(), timeout
        self.adapter = adapter.resolve() if adapter else None
        # 생성 이력에서 어댑터 적용 여부를 구분할 수 있도록 모델 이름에 함께 남긴다.
        self.model_id = self.path.name + (f"+{self.adapter.name}" if self.adapter else "")
        self.last_source_guard = None

    def validate_config(self):
        if not (self.path / "config.json").is_file() or not list(self.path.glob("*.safetensors")):
            raise SearchUnavailable("LLM_MODEL_PATH에 로컬 Qwen 모델 파일을 준비해 주세요.")
        if self.timeout <= 0:
            raise SearchUnavailable("LLM_TIMEOUT_SECONDS는 양수여야 합니다.")
        if self.adapter:
            self._validate_adapter()

    def _validate_adapter(self):
        # 다른 프롬프트 버전으로 학습한 어댑터는 입력 형식이 달라 품질을 보장할 수 없다.
        meta = self.adapter / "training-meta.json"
        if not (self.adapter / "adapter_config.json").is_file() or not meta.is_file():
            raise SearchUnavailable("LLM_ADAPTER_PATH에 학습된 어댑터 파일을 준비해 주세요.")
        try:
            trained_for = json.loads(meta.read_text(encoding="utf-8"))["prompt_version"]
        except (ValueError, KeyError) as exc:
            raise SearchUnavailable("어댑터 학습 정보를 읽을 수 없습니다.") from exc
        if trained_for != self.prompt_version:
            raise SearchUnavailable(
                "어댑터가 현재 프롬프트 버전과 맞지 않습니다. 다시 학습해 주세요."
            )

    async def select(self, query, blocks, *, retry=False):
        # 웹 서버와 동일한 가상환경의 Python을 사용한다. stdin/stdout만 계약으로
        # 삼아 경로·원문을 명령 문자열에 삽입하지 않고, 셸을 거치지 않고 실행한다.
        self.validate_config()
        self.last_source_guard = None
        payload = json.dumps({"query": query, "blocks": blocks, "retry": retry}, ensure_ascii=False)
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "handover_ai.adapters.qwen_worker",
            str(self.path),
            *([str(self.adapter)] if self.adapter else []),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            output, _ = await asyncio.wait_for(process.communicate(payload.encode()), self.timeout)
        except (TimeoutError, asyncio.CancelledError):
            # 서비스의 합산 시간 제한으로 취소돼도 하위 추론을 남겨 두지 않는다.
            # kill 후 wait까지 마쳐 GPU 메모리와 자식 프로세스를 회수한다.
            await terminate_worker(process)
            raise
        if process.returncode:
            raise DraftFailure("model_unavailable")
        try:
            envelope = json.loads(output)
            self.last_source_guard = envelope.get("source_guard")
            if envelope.get("error"):
                raise DraftFailure(envelope["error"])
            return DraftSelection.model_validate(envelope["selection"])
        except (ValueError, KeyError, TypeError) as exc:
            raise DraftFailure("invalid_model_output") from exc
