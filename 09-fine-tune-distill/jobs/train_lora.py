"""LoRA fine-tuning of a small open model on the distillation set (GPU; not run in this lesson's tests).

    uv sync --group train
    uv run python jobs/train_lora.py --model Qwen/Qwen3-1.7B --data .data/train.jsonl --out .data/lora-triage

On one L40S or A10G this takes minutes for ~3k short examples. Run it as a RayJob on your KubeRay cluster
(Ray Train wraps the same script) or as a plain Kubernetes Job with one GPU.

LoRA trains small low-rank adapter matrices instead of all weights: a few MB to store, one base model in
GPU memory can serve many adapters (k8s/vllm-multi-lora.yaml), and the base model's general ability is
largely preserved. Loss is computed on the assistant turn only, so the model learns to answer, not to
repeat tickets.
"""
from __future__ import annotations

import argparse

from datasets import load_dataset
from peft import LoraConfig
from trl import SFTConfig, SFTTrainer


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="Qwen/Qwen3-1.7B")
    ap.add_argument("--data", default=".data/train.jsonl")
    ap.add_argument("--out", default=".data/lora-triage")
    ap.add_argument("--epochs", type=float, default=2)
    a = ap.parse_args()

    ds = load_dataset("json", data_files=a.data, split="train").remove_columns(["meta"])
    split = ds.train_test_split(test_size=0.05, seed=7)

    trainer = SFTTrainer(
        model=a.model,
        train_dataset=split["train"],
        eval_dataset=split["test"],
        args=SFTConfig(
            output_dir=a.out,
            num_train_epochs=a.epochs,
            per_device_train_batch_size=16,
            gradient_accumulation_steps=2,
            learning_rate=2e-4,              # LoRA tolerates higher LRs than full fine-tuning
            lr_scheduler_type="cosine",
            warmup_ratio=0.05,
            max_length=512,
            assistant_only_loss=True,        # learn the answer, not the prompt
            bf16=True,
            logging_steps=10,
            eval_strategy="epoch",
            save_strategy="epoch",
            report_to="none",
        ),
        peft_config=LoraConfig(r=16, lora_alpha=32, lora_dropout=0.05, target_modules="all-linear",
                               task_type="CAUSAL_LM"),
    )
    trainer.train()
    trainer.save_model(a.out)            # saves only the adapter (a few MB), not the base model


if __name__ == "__main__":
    main()
