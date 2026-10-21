"""LoRA fine-tuning of the local chat model with Unsloth (NVIDIA GPU, Windows or Linux).

    python tools/finetune/train_lora.py --smoke                      # does it fit? 10 steps, longest rows
    python tools/finetune/train_lora.py --data data/train/train.jsonl --epochs 2
    python tools/finetune/train_lora.py --data ... --export merged   # also write the merged 16-bit model

Runs in its own environment, not the bot's:
Python 3.12, PyTorch with CUDA, then ``pip install unsloth`` (Transformers v5).

``--data`` is JSONL from tools/make_training_data.py, one chat per line:
``{"messages": [{"role": "system", ...}, {"role": "user", ...}, {"role": "assistant", ...}]}``.
Only the assistant's answer is trained (train_on_responses_only).  Thinking is
off in the chat template, as the bot runs the model.

``--smoke`` needs no data: it builds rows exactly ``--max-seq`` tokens long (the
worst case for memory) and trains ``--steps`` steps.  Either way the run ends
with one JSON line: peak GPU memory, seconds per step, first and last loss.

The RTX 4050 Laptop has 6 GB: Unsloth's figure for a 4B bf16 LoRA is about
10 GB, so the default here is 4-bit (QLoRA), which Unsloth does not recommend
for Qwen3.5 (larger quantization error).  The smoke run says whether it fits;
the evaluation says whether the quality is good enough.
"""

from __future__ import annotations

import argparse
import inspect
import json
import os
import time
from pathlib import Path

DEFAULT_MODEL = "unsloth/Qwen3.5-4B"
TARGET_MODULES = ["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"]
# Qwen's chat template: where a user turn and an assistant turn start.
INSTRUCTION_PART = "<|im_start|>user\n"
RESPONSE_PART = "<|im_start|>assistant\n"

SMOKE_SYSTEM = "你是杨宇靖，微信群「宿舍夜聊」里的普通群友。说话短、反应快、判断直接。只输出一个 JSON 对象。"
SMOKE_LINE = "阿明: 周末要不要一起去图书馆复习，顺便吃个饭，我知道楼下新开了一家面馆还挺好吃的\n"
SMOKE_ANSWER = '{"addressee":"me","action":"reply","messages":["好呀，几点集合"],"reason":"被约周末复习"}'


def load_rows(path: str | Path) -> list[dict]:
    """The chats of a training file; a line without a final assistant answer is an error."""
    rows = []
    for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        messages = row.get("messages") or []
        if not messages or messages[-1].get("role") != "assistant" or not messages[-1].get("content"):
            raise ValueError(f"{path}:{number}: the last message must be the assistant's answer")
        rows.append({"messages": messages})
    if not rows:
        raise ValueError(f"{path}: no rows")
    return rows


def render(tokenizer, messages: list[dict]) -> str:
    """One chat as training text, thinking off (the way the bot runs the model)."""
    try:
        return tokenizer.apply_chat_template(messages, tokenize=False, enable_thinking=False)
    except TypeError:                                     # a template without the switch
        return tokenizer.apply_chat_template(messages, tokenize=False)


def text_tokenizer(tokenizer):
    """The text tokenizer: for Qwen3.5, Unsloth returns a processor for images
    and text whose first argument is ``images`` (round 4: a training text sent
    there failed with "Incorrect image source"); the tokenizer is inside it."""
    return getattr(tokenizer, "tokenizer", tokenizer)


def smoke_rows(tokenizer, max_seq: int, count: int) -> list[dict]:
    """``count`` chats about ``max_seq`` tokens long."""
    count_tokens = text_tokenizer(tokenizer)
    user = ""
    while True:
        longer = user + SMOKE_LINE
        messages = [{"role": "system", "content": SMOKE_SYSTEM},
                    {"role": "user", "content": longer + "最新消息来自 阿明：@杨宇靖 你去吗"},
                    {"role": "assistant", "content": SMOKE_ANSWER}]
        if len(count_tokens(render(tokenizer, messages))["input_ids"]) > max_seq - 8:
            break
        user = longer
    messages[1]["content"] = user + "最新消息来自 阿明：@杨宇靖 你去吗"
    return [{"messages": [dict(m) for m in messages]} for _ in range(count)]


def fitting_rows(tokenizer, rows: list[dict], max_seq: int) -> tuple[list[dict], int]:
    """The rows whose whole chat fits in ``max_seq`` tokens, and how many did not.
    A longer row would be cut at the end, which is where the answer is."""
    count_tokens = text_tokenizer(tokenizer)
    kept = [r for r in rows if len(count_tokens(render(tokenizer, r["messages"]))["input_ids"]) <= max_seq]
    return kept, len(rows) - len(kept)


def configure_memory(ce_chunks: int, environ=os.environ) -> None:
    """Settings that must be in place before torch and Unsloth are imported.

    Unsloth's fused cross entropy measures the free GPU memory and refuses to
    run when it is close to nothing (round 4b on the 6 GB 4050: "No or
    negligible GPU memory available for fused cross entropy" at 5.8 GB used).
    ``ce_chunks`` fixes the number of chunks instead (UNSLOTH_CE_LOSS_N_CHUNKS),
    so the loss works in slices of a few tokens.  Expandable segments make the
    allocator waste less memory on fragments.
    """
    if ce_chunks > 0:
        environ["UNSLOTH_CE_LOSS_N_CHUNKS"] = str(ce_chunks)
    environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")


def _accepted(callable_, **kwargs) -> dict:
    """The keyword arguments ``callable_`` takes (TRL renames them between versions)."""
    names = inspect.signature(callable_).parameters
    return {k: v for k, v in kwargs.items() if k in names}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--data", help="training JSONL (tools/make_training_data.py)")
    parser.add_argument("--smoke", action="store_true", help="no data: rows of --max-seq tokens, --steps steps")
    parser.add_argument("--bits", type=int, choices=(4, 16), default=4, help="4 = QLoRA (default), 16 = bf16 LoRA")
    parser.add_argument("--max-seq", type=int, default=2048)
    parser.add_argument("--rank", type=int, default=16)
    parser.add_argument("--alpha", type=int, default=16)
    parser.add_argument("--lr", type=float, default=2e-4)
    parser.add_argument("--epochs", type=float, default=2)
    parser.add_argument("--steps", type=int, default=0, help="stop after this many steps (smoke default 10)")
    parser.add_argument("--accum", type=int, default=8, help="gradient accumulation (batch size is 1)")
    parser.add_argument("--out", default="data/train/run")
    parser.add_argument("--ce-chunks", type=int, default=0,
                        help="compute the loss in this many slices (e.g. 128) instead of letting "
                             "Unsloth measure the free memory; for a small GPU")
    parser.add_argument("--export", choices=("none", "merged"), default="none",
                        help="merged: also write the merged 16-bit model for the GGUF conversion")
    args = parser.parse_args(argv)
    if not args.smoke and not args.data:
        parser.error("give --data, or --smoke to test the memory")
    configure_memory(args.ce_chunks)

    from unsloth import FastLanguageModel                 # before transformers / trl, as Unsloth asks
    from unsloth.chat_templates import train_on_responses_only
    import torch
    from datasets import Dataset
    from trl import SFTConfig, SFTTrainer

    if not torch.cuda.is_available():
        raise SystemExit("no CUDA GPU visible to PyTorch: check the driver and the CUDA build of torch")
    model, tokenizer = FastLanguageModel.from_pretrained(
        model_name=args.model, max_seq_length=args.max_seq,
        load_in_4bit=args.bits == 4, load_in_16bit=args.bits == 16, full_finetuning=False,
    )
    model = FastLanguageModel.get_peft_model(
        model, r=args.rank, target_modules=TARGET_MODULES, lora_alpha=args.alpha, lora_dropout=0,
        bias="none", use_gradient_checkpointing="unsloth", random_state=3407, max_seq_length=args.max_seq,
    )
    too_long = 0
    if args.smoke:
        rows = smoke_rows(tokenizer, args.max_seq, 16)
    else:
        rows, too_long = fitting_rows(tokenizer, load_rows(args.data), args.max_seq)
        print(f"{len(rows)} rows fit in {args.max_seq} tokens; {too_long} longer ones left out", flush=True)
        if not rows:
            raise SystemExit("no row fits: raise --max-seq or shorten the prompts")
    dataset = Dataset.from_dict({"text": [render(tokenizer, r["messages"]) for r in rows]})
    steps = args.steps or (10 if args.smoke else 0)
    config = SFTConfig(**_accepted(
        SFTConfig.__init__,
        dataset_text_field="text", max_seq_length=args.max_seq, max_length=args.max_seq,
        per_device_train_batch_size=1, gradient_accumulation_steps=1 if args.smoke else args.accum,
        warmup_ratio=0.05, num_train_epochs=args.epochs, max_steps=steps or -1,
        learning_rate=args.lr, weight_decay=0.01, lr_scheduler_type="linear", optim="adamw_8bit",
        logging_steps=1, seed=3407, output_dir=args.out, report_to="none", save_strategy="no",
        bf16=torch.cuda.is_bf16_supported(), fp16=not torch.cuda.is_bf16_supported(),
        dataset_num_proc=1,                               # Windows: no worker processes
    ))
    trainer = SFTTrainer(model=model, train_dataset=dataset, args=config,
                         **_accepted(SFTTrainer.__init__, processing_class=tokenizer, tokenizer=tokenizer))
    trainer = train_on_responses_only(trainer, instruction_part=INSTRUCTION_PART, response_part=RESPONSE_PART)

    torch.cuda.reset_peak_memory_stats()
    started = time.time()
    result = trainer.train()
    seconds = time.time() - started
    losses = [entry["loss"] for entry in trainer.state.log_history if "loss" in entry]
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    if not args.smoke:
        model.save_pretrained(str(out / "lora"))
        tokenizer.save_pretrained(str(out / "lora"))
        if args.export == "merged":
            model.save_pretrained_merged(str(out / "merged"), tokenizer, save_method="merged_16bit")
    summary = {
        "model": args.model, "bits": args.bits, "max_seq": args.max_seq, "rows": len(rows),
        "rows_too_long": too_long, "ce_chunks": args.ce_chunks, "rank": args.rank,
        "steps": result.global_step, "seconds": round(seconds, 1),
        "seconds_per_step": round(seconds / max(1, result.global_step), 2),
        "peak_gpu_gb": round(torch.cuda.max_memory_reserved() / 2**30, 2),
        "gpu": torch.cuda.get_device_name(0),
        "gpu_total_gb": round(torch.cuda.get_device_properties(0).total_memory / 2**30, 2),
        "loss_first": losses[0] if losses else None, "loss_last": losses[-1] if losses else None,
    }
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
