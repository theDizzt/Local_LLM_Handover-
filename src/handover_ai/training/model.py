"""학습 파이프라인 전용 Qwen 실행: 합성 질문 생성과 QLoRA 어댑터 학습.

운영 Worker와 달리 한 프로세스에서 모델을 한 번만 올려 여러 요청을 처리한다.
모델 파일은 로컬에서만 읽고 원격 코드 실행을 허용하지 않는다.
"""

import hashlib
import json
import math
import random
from pathlib import Path
from time import perf_counter

LORA_TARGETS = ["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"]


def model_hashes(path: Path):
    return {
        name: hashlib.sha256((path / name).read_bytes()).hexdigest()
        for name in ("config.json", "model.safetensors.index.json")
        if (path / name).is_file()
    }


def quantization(torch):
    from transformers import BitsAndBytesConfig

    return BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_use_double_quant=True,
        bnb_4bit_compute_dtype=torch.bfloat16,
    )


class Generator:
    """합성 질문 생성과 무답 질문 확인에 쓰는 기반 모델(어댑터 없음)."""

    def __init__(self, path: Path, seed: int):
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        if not torch.cuda.is_available():
            raise RuntimeError("CUDA GPU가 필요합니다.")
        torch.manual_seed(seed)
        self.torch = torch
        self.tokenizer = AutoTokenizer.from_pretrained(
            path, local_files_only=True, trust_remote_code=False
        )
        # 4bit로 올린다. 다른 앱이 GPU를 쓰는 12GB 장비에서 bf16 8GB가 공유 메모리로
        # 넘치면 생성이 수십 배 느려진다. 질문 생성 품질에는 큰 차이가 없다.
        self.model = AutoModelForCausalLM.from_pretrained(
            path,
            local_files_only=True,
            trust_remote_code=False,
            dtype=torch.bfloat16,
            device_map="cuda:0",
            quantization_config=quantization(torch),
        )
        self.model.eval()

    def complete(self, messages, *, max_new_tokens=400, sample=True, thinking=False):
        prompt = self.tokenizer.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True, enable_thinking=thinking
        )
        inputs = self.tokenizer(prompt, return_tensors="pt").to(self.model.device)
        options = {"do_sample": False}
        if sample:
            # Qwen3 비사고 모드 권장값. 같은 원문에서도 질문 표현이 한 가지로 굳지 않게 한다.
            options = {"do_sample": True, "temperature": 0.7, "top_p": 0.8, "top_k": 20}
        with self.torch.inference_mode():
            ids = self.model.generate(
                **inputs,
                max_new_tokens=max_new_tokens,
                pad_token_id=self.tokenizer.eos_token_id,
                **options,
            )
        text = self.tokenizer.decode(ids[0, inputs.input_ids.shape[1] :], skip_special_tokens=True)
        if thinking and "</think>" in text:
            text = text.split("</think>", 1)[1]
        return text.strip()


def encode(tokenizer, example, max_tokens):
    """운영과 같은 채팅 템플릿으로 입력을 만들고 답 부분만 손실 대상으로 둔다."""
    prompt = tokenizer.apply_chat_template(
        example["messages"], tokenize=False, add_generation_prompt=True, enable_thinking=False
    )
    prompt_ids = tokenizer(prompt, add_special_tokens=False).input_ids
    target_ids = tokenizer(example["target"] + "<|im_end|>", add_special_tokens=False).input_ids
    if len(prompt_ids) + len(target_ids) > max_tokens:
        return None
    return prompt_ids, target_ids


def train_adapter(train_rows, val_rows, base: Path, output: Path, options, prompt_version, log):
    """4bit 기반 모델 위에 LoRA를 학습하고 검증 손실이 가장 낮은 epoch를 저장한다."""
    import torch
    from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training
    from transformers import (
        AutoModelForCausalLM,
        AutoTokenizer,
        get_cosine_schedule_with_warmup,
    )

    if not torch.cuda.is_available():
        raise RuntimeError("CUDA GPU가 필요합니다.")
    random.seed(options["seed"])
    torch.manual_seed(options["seed"])
    tokenizer = AutoTokenizer.from_pretrained(base, local_files_only=True, trust_remote_code=False)
    model = AutoModelForCausalLM.from_pretrained(
        base,
        local_files_only=True,
        trust_remote_code=False,
        dtype=torch.bfloat16,
        device_map="cuda:0",
        quantization_config=quantization(torch),
    )
    model = prepare_model_for_kbit_training(model, use_gradient_checkpointing=True)
    model = get_peft_model(
        model,
        LoraConfig(
            r=options["rank"],
            lora_alpha=options["rank"] * 2,
            lora_dropout=0.05,
            target_modules=LORA_TARGETS,
            task_type="CAUSAL_LM",
        ),
    )

    def prepare(rows):
        encoded, skipped = [], 0
        for row in rows:
            pair = encode(tokenizer, row, options["max_tokens"])
            if pair is None:
                skipped += 1
            else:
                encoded.append((row["task"], *pair))
        return encoded, skipped

    train, train_skipped = prepare(train_rows)
    val, val_skipped = prepare(val_rows)
    log(
        f"학습 예제 {len(train)}개, 검증 예제 {len(val)}개 (길이 초과 제외 "
        f"{train_skipped}/{val_skipped}개)"
    )
    if not train:
        raise RuntimeError("학습할 예제가 없습니다.")

    def loss_of(prompt_ids, target_ids):
        ids = torch.tensor([prompt_ids + target_ids], device=model.device)
        # 답 위치의 logits만 계산한다. 전체 길이의 어휘 logits는 12GB GPU에 들어가지 않는다.
        logits = model(input_ids=ids, logits_to_keep=len(target_ids) + 1).logits[0, :-1]
        labels = torch.tensor(target_ids, device=model.device)
        loss = torch.nn.functional.cross_entropy(logits.float(), labels)
        exact = bool((logits.argmax(-1) == labels).all())
        return loss, exact

    def evaluate():
        model.eval()
        losses, exact = [], {}
        with torch.no_grad():
            for task, prompt_ids, target_ids in val:
                loss, hit = loss_of(prompt_ids, target_ids)
                losses.append(loss.item())
                exact.setdefault(task, []).append(hit)
        model.train()
        return {
            "loss": sum(losses) / len(losses) if losses else None,
            # 교사 강제 상태에서 모든 답 토큰이 최댓값과 같으면 greedy 생성 결과와 같다.
            "exact": {task: sum(v) / len(v) for task, v in exact.items()},
        }

    parameters = [p for p in model.parameters() if p.requires_grad]
    optimizer = torch.optim.AdamW(parameters, lr=options["learning_rate"], weight_decay=0.0)
    steps = math.ceil(len(train) / options["grad_accum"]) * options["epochs"]
    scheduler = get_cosine_schedule_with_warmup(optimizer, max(1, int(steps * 0.03)), steps)
    history, best, out_of_memory = [], None, 0
    model.train()
    for epoch in range(1, options["epochs"] + 1):
        random.shuffle(train)
        started, running, pending = perf_counter(), [], 0
        for index, (_, prompt_ids, target_ids) in enumerate(train, 1):
            try:
                loss, _ = loss_of(prompt_ids, target_ids)
                (loss / options["grad_accum"]).backward()
                running.append(loss.item())
                pending += 1
            except torch.cuda.OutOfMemoryError:
                # 긴 예제 하나 때문에 전체 학습을 버리지 않는다. 건너뛴 수를 기록한다.
                out_of_memory += 1
                optimizer.zero_grad(set_to_none=True)
                pending = 0
                torch.cuda.empty_cache()
                continue
            if pending == options["grad_accum"] or index == len(train):
                torch.nn.utils.clip_grad_norm_(parameters, 1.0)
                optimizer.step()
                scheduler.step()
                optimizer.zero_grad(set_to_none=True)
                pending = 0
            if index % 50 == 0:
                recent = running[-50:]
                log(
                    f"epoch {epoch} {index}/{len(train)} loss {sum(recent) / len(recent):.4f} "
                    f"{perf_counter() - started:.0f}s"
                )
        metrics = evaluate() if val else {"loss": None, "exact": {}}
        record = {
            "epoch": epoch,
            "train_loss": sum(running) / len(running) if running else None,
            "val": metrics,
            "seconds": round(perf_counter() - started, 1),
        }
        history.append(record)
        log(json.dumps(record, ensure_ascii=False))
        score = metrics["loss"] if metrics["loss"] is not None else -epoch
        if best is None or score < best:
            best = score
            model.save_pretrained(output)
    meta = {
        "prompt_version": prompt_version,
        "base_model": str(base.resolve()),
        "base_model_hashes": model_hashes(base),
        "options": options,
        "train_examples": len(train),
        "val_examples": len(val),
        "skipped_too_long": [train_skipped, val_skipped],
        "skipped_out_of_memory": out_of_memory,
        "history": history,
    }
    (output / "training-meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return meta
