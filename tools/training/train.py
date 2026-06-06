"""Model tuning script for MTG Arena coach.

Performs SFT (Supervised Fine-Tuning) or DPO (Direct Preference Optimization)
LoRA fine-tuning on compiled self-play datasets.

Prerequisites:
    pip install transformers peft trl accelerate torch datasets

Usage:
    python -m tools.training.train \\
        --model_id google/gemma-4-E2B-it \\
        --dataset tools/training/data/dpo_dataset.json \\
        --output_dir tools/training/checkpoints/gemma4_dpo \\
        --method dpo \\
        --epochs 2
"""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path
import torch
from datasets import load_dataset
from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training
from transformers import (
    AutoModelForCausalLM,
    AutoTokenizer,
    BitsAndBytesConfig,
    TrainingArguments,
)
from trl import DPOTrainer, SFTTrainer, SFTConfig, DPOConfig

logger = logging.getLogger("tools.training.train")


def main():
    p = argparse.ArgumentParser(description="Fine-tune MTGA coach model.")
    p.add_argument("--model_id", default="google/gemma-4-E2B-it", help="Base model ID or path")
    p.add_argument("--dataset", required=True, type=Path, help="Path to JSON dataset")
    p.add_argument("--output_dir", required=True, type=Path, help="Checkpoints output directory")
    p.add_argument("--method", choices=["sft", "dpo"], default="dpo", help="Training method")
    p.add_argument("--epochs", type=int, default=1, help="Number of training epochs")
    p.add_argument("--batch_size", type=int, default=2, help="Batch size per device")
    p.add_argument("--lr", type=float, default=1e-4, help="Learning rate")
    p.add_argument("--lora_r", type=int, default=8, help="LoRA rank")
    p.add_argument("--lora_alpha", type=int, default=16, help="LoRA alpha")
    p.add_argument("--load_in_4bit", action="store_true", help="Load base model in 4bit quantization")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s | %(message)s")

    logger.info(f"Loading dataset: {args.dataset}")
    # Load dataset from JSON
    dataset = load_dataset("json", data_files=str(args.dataset))

    # Format dataset depending on SFT or DPO
    # DPOTrainer expects dict with keys: 'prompt', 'chosen', 'rejected'
    # SFTTrainer expects dict with keys: 'prompt', 'response' or a text field

    logger.info(f"Loading tokenizer: {args.model_id}")
    tokenizer = AutoTokenizer.from_pretrained(args.model_id)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    # Setup quantization if specified
    bnb_config = None
    if args.load_in_4bit:
        logger.info("Enabling 4bit quantization")
        bnb_config = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_use_double_quant=True,
            bnb_4bit_compute_dtype=torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16,
        )

    logger.info(f"Loading base model: {args.model_id}")
    model = AutoModelForCausalLM.from_pretrained(
        args.model_id,
        quantization_config=bnb_config,
        device_map="auto",
        torch_dtype=torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16,
    )

    if args.load_in_4bit:
        model = prepare_model_for_kbit_training(model)

    # Setup LoRA
    peft_config = LoraConfig(
        r=args.lora_r,
        lora_alpha=args.lora_alpha,
        lora_dropout=0.05,
        bias="none",
        task_type="CAUSAL_LM",
        target_modules=["q_proj", "o_proj", "k_proj", "v_proj", "gate_proj", "up_proj", "down_proj"],
    )

    if args.method == "sft":
        logger.info("Initializing SFTTrainer")

        def formatting_prompts_func(example):
            return f"{example['system']}\n{example['user']}\nResponse: {example['response']}"

        training_args = SFTConfig(
            output_dir=str(args.output_dir),
            num_train_epochs=args.epochs,
            per_device_train_batch_size=args.batch_size,
            gradient_accumulation_steps=4,
            learning_rate=args.lr,
            logging_steps=10,
            save_strategy="epoch",
            eval_strategy="no",
            fp16=not torch.cuda.is_bf16_supported(),
            bf16=torch.cuda.is_bf16_supported(),
            optim="paged_adamw_32bit" if args.load_in_4bit else "adamw_torch",
            remove_unused_columns=False,
            max_length=2048,
        )

        trainer = SFTTrainer(
            model=model,
            train_dataset=dataset["train"],
            peft_config=peft_config,
            processing_class=tokenizer,
            args=training_args,
            formatting_func=formatting_prompts_func,
        )
    elif args.method == "dpo":
        logger.info("Initializing DPOTrainer")
        
        # DPOTrainer natively handles prompt, chosen, rejected fields. We only
        # need to verify the fields exist.
        for col in ["prompt_system", "prompt_user", "chosen", "rejected"]:
            if col not in dataset["train"].column_names and col == "prompt_system":
                # compatibility renaming
                dataset = dataset.rename_column("system", "prompt_system")
            if col not in dataset["train"].column_names and col == "prompt_user":
                dataset = dataset.rename_column("user", "prompt_user")

        # Map dataset to prompt, chosen, rejected
        def map_dpo_fields(examples):
            prompts = []
            for sys_p, usr_p in zip(examples["prompt_system"], examples["prompt_user"]):
                prompts.append(f"{sys_p}\n{usr_p}")
            return {
                "prompt": prompts,
                "chosen": examples["chosen"],
                "rejected": examples["rejected"],
            }

        dpo_dataset = dataset["train"].map(map_dpo_fields, batched=True)

        training_args = DPOConfig(
            output_dir=str(args.output_dir),
            num_train_epochs=args.epochs,
            per_device_train_batch_size=args.batch_size,
            gradient_accumulation_steps=4,
            learning_rate=args.lr,
            logging_steps=10,
            save_strategy="epoch",
            eval_strategy="no",
            fp16=not torch.cuda.is_bf16_supported(),
            bf16=torch.cuda.is_bf16_supported(),
            optim="paged_adamw_32bit" if args.load_in_4bit else "adamw_torch",
            remove_unused_columns=False,
            max_length=2048,
            beta=0.1,
        )

        trainer = DPOTrainer(
            model=model,
            ref_model=None,  # DPOTrainer disables ref model internally when peft is used to save memory
            args=training_args,
            train_dataset=dpo_dataset,
            processing_class=tokenizer,
            peft_config=peft_config,
        )

    logger.info("Starting training...")
    trainer.train()

    logger.info(f"Saving fine-tuned adapter model to {args.output_dir}")
    trainer.model.save_pretrained(str(args.output_dir))
    tokenizer.save_pretrained(str(args.output_dir))
    logger.info("Training completed successfully!")


if __name__ == "__main__":
    main()
