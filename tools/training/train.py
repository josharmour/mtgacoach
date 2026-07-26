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

A --val_split fraction (default 5%) is held out and, unless
--early_stopping_patience 0 is passed, training stops after that many
evaluations without an eval_loss improvement and restores the best checkpoint.
See resolve_eval_save_strategy() for how the eval/save cadences are reconciled.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Any

# NOTE: torch / transformers / trl / peft / datasets are imported inside main().
# They are heavyweight GPU-host dependencies; keeping them out of module import
# lets the pure training-config helpers below (and their regression tests) run
# anywhere. formatting.py is dependency-free and stays at module scope.
from tools.training.formatting import derive_turn_end, render_serve_prompt

logger = logging.getLogger("tools.training.train")

# Default early-stopping patience: number of consecutive evaluations without a
# `metric_for_best_model` improvement before training halts.
DEFAULT_EARLY_STOPPING_PATIENCE = 3


def resolve_eval_save_strategy(
    save_steps: int,
    has_eval: bool,
    early_stopping_patience: int,
    save_total_limit: int,
    metric_for_best_model: str = "eval_loss",
) -> dict[str, Any]:
    """Resolve the eval/save/early-stopping trainer settings consistently.

    ``EarlyStoppingCallback`` only makes sense together with
    ``load_best_model_at_end=True`` (otherwise training stops early *and* keeps
    the last, already-overfitting weights). HF's ``TrainingArguments`` validates
    that combination strictly:

      * ``eval_strategy`` must equal ``save_strategy``; and
      * for ``"steps"``, ``save_steps`` must be a whole multiple of ``eval_steps``.

    The pre-existing defaults violate both (``save_strategy="steps"`` with
    ``save_steps=25`` vs ``eval_strategy="epoch"``), so the trainer would raise
    at construction. Resolution: **keep the caller's ``--save_steps`` cadence and
    move evaluation onto it** (``eval_steps = save_steps``). Checkpoint cadence
    is what the operator tuned for their run length, and evaluating exactly when
    we checkpoint guarantees the best checkpoint is one we actually kept — which
    is what ``load_best_model_at_end`` needs. Evaluating per-epoch instead would
    give early stopping only one signal per epoch, useless for the 1-2 epoch runs
    this script is used for.

    With ``save_steps == 0`` (per-epoch checkpointing) both move to ``"epoch"``.
    With no eval split, or patience <= 0, early stopping is off and the original
    save behaviour is preserved untouched.
    """
    cfg: dict[str, Any] = {
        "save_strategy": "steps" if save_steps > 0 else "epoch",
        "save_steps": save_steps if save_steps > 0 else 500,
        "save_total_limit": save_total_limit,
        "eval_strategy": "epoch" if has_eval else "no",
        "eval_steps": None,
        "load_best_model_at_end": False,
        "metric_for_best_model": None,
        "greater_is_better": None,
        "early_stopping": False,
    }

    if not has_eval:
        if early_stopping_patience > 0:
            logger.warning(
                "Early stopping requested but --val_split produced no eval set; "
                "early stopping is DISABLED for this run."
            )
        return cfg

    if early_stopping_patience <= 0:
        return cfg

    if save_steps > 0:
        cfg["eval_strategy"] = "steps"
        cfg["eval_steps"] = save_steps
    else:
        cfg["eval_strategy"] = "epoch"

    cfg["load_best_model_at_end"] = True
    cfg["metric_for_best_model"] = metric_for_best_model
    cfg["greater_is_better"] = not metric_for_best_model.endswith("loss")
    cfg["early_stopping"] = True
    # load_best_model_at_end needs room for the best checkpoint alongside the
    # most recent one; a limit of 1 would rotate the best checkpoint away.
    cfg["save_total_limit"] = max(2, save_total_limit)
    return cfg


def main():
    p = argparse.ArgumentParser(description="Fine-tune MTGA coach model.")
    p.add_argument("--model_id", default="google/gemma-4-E2B-it", help="Base model ID or path")
    p.add_argument("--dataset", required=True, type=Path, help="Path to JSON dataset")
    p.add_argument("--output_dir", required=True, type=Path, help="Checkpoints output directory")
    p.add_argument("--method", choices=["sft", "dpo"], default="dpo", help="Training method")
    p.add_argument("--epochs", type=int, default=1, help="Number of training epochs")
    p.add_argument("--batch_size", type=int, default=2, help="Batch size per device")
    p.add_argument("--gradient_accumulation_steps", type=int, default=4, help="Gradient accumulation steps")
    p.add_argument("--gradient_checkpointing", action="store_true", help="Enable gradient checkpointing")
    p.add_argument("--lr", type=float, default=1e-4, help="Learning rate")
    p.add_argument("--lora_r", type=int, default=8, help="LoRA rank")
    p.add_argument("--lora_alpha", type=int, default=16, help="LoRA alpha")
    p.add_argument("--load_in_4bit", action="store_true", help="Load base model in 4bit quantization")
    p.add_argument("--max_length", type=int, default=4096, help="Max token sequence length")
    p.add_argument("--val_split", type=float, default=0.05, help="Validation set split ratio (e.g. 0.05)")
    p.add_argument(
        "--save_steps", type=int, default=25, help="Checkpoint every N optimizer steps (0 = per-epoch only)"
    )
    p.add_argument("--save_total_limit", type=int, default=3, help="Max checkpoints to retain")
    p.add_argument(
        "--early_stopping_patience",
        type=int,
        default=DEFAULT_EARLY_STOPPING_PATIENCE,
        help="Stop after N evaluations without improvement (0 disables early stopping)",
    )
    p.add_argument(
        "--early_stopping_threshold",
        type=float,
        default=0.0,
        help="Minimum improvement in the best metric to reset the patience counter",
    )
    p.add_argument(
        "--metric_for_best_model",
        default="eval_loss",
        help="Metric that selects the best checkpoint for early stopping",
    )
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s | %(message)s")

    import torch
    from datasets import load_dataset
    from peft import LoraConfig, prepare_model_for_kbit_training
    from transformers import (
        AutoModelForCausalLM,
        AutoTokenizer,
        BitsAndBytesConfig,
        EarlyStoppingCallback,
    )
    from trl import DPOConfig, DPOTrainer, SFTConfig, SFTTrainer

    logger.info(f"Loading dataset: {args.dataset}")
    # Load dataset from JSON
    full_dataset = load_dataset("json", data_files=str(args.dataset))

    if args.val_split > 0.0 and "train" in full_dataset:
        split_data = full_dataset["train"].train_test_split(test_size=args.val_split, seed=42)
        train_ds = split_data["train"]
        eval_ds = split_data["test"]
    else:
        train_ds = full_dataset["train"]
        eval_ds = None

    # Resolve eval/save cadence + early stopping once, so SFT and DPO agree and
    # so load_best_model_at_end never fights TrainingArguments' validation.
    sched = resolve_eval_save_strategy(
        save_steps=args.save_steps,
        has_eval=eval_ds is not None,
        early_stopping_patience=args.early_stopping_patience,
        save_total_limit=args.save_total_limit,
        metric_for_best_model=args.metric_for_best_model,
    )
    logger.info(
        f"Schedule: save={sched['save_strategy']}({sched['save_steps']}) "
        f"eval={sched['eval_strategy']}({sched['eval_steps']}) "
        f"early_stopping={sched['early_stopping']} "
        f"(patience={args.early_stopping_patience}, metric={args.metric_for_best_model})"
    )

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
        dtype=torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16,
        attn_implementation="sdpa",
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
        target_modules="(.*language_model.*(q_proj|k_proj|v_proj|o_proj|gate_proj|up_proj|down_proj))",
    )

    if args.method == "sft":
        logger.info("Initializing SFTTrainer")

        # Serve parity (WP-0.2) + completion-only loss.
        #
        # The sample must still be byte-identical to the serve-time render,
        # but it is split across TRL's `prompt`/`completion` columns rather
        # than emitted as one blob by a formatting_func. That split is what
        # makes TRL mask the prompt out of the loss: it resolves
        # completion_only_loss by checking for those two columns, and it
        # explicitly refuses to combine completion masking with a
        # formatting_func. Concatenated, prompt + completion reproduces
        # format_sft_example() exactly (asserted in the parity tests).
        #
        # Without this, labels == input_ids and ~98% of the gradient trains
        # the model to regenerate the game-state prompt (mean 1,757 prompt
        # tokens vs 33 answer tokens) instead of learning the answer.
        turn_end = derive_turn_end(tokenizer)

        def to_prompt_completion(example):
            return {
                "prompt": render_serve_prompt(
                    tokenizer,
                    example.get("system") or "",
                    example.get("user") or "",
                ),
                "completion": (example.get("response") or "") + turn_end,
            }

        _drop = [c for c in train_ds.column_names if c not in ("prompt", "completion")]
        train_ds = train_ds.map(to_prompt_completion, remove_columns=_drop)
        if eval_ds is not None:
            eval_ds = eval_ds.map(to_prompt_completion, remove_columns=_drop)
        logger.info(f"SFT columns after mapping: {train_ds.column_names} (prompt masked from loss)")

        training_args = SFTConfig(
            output_dir=str(args.output_dir),
            num_train_epochs=args.epochs,
            per_device_train_batch_size=args.batch_size,
            gradient_accumulation_steps=args.gradient_accumulation_steps,
            gradient_checkpointing=args.gradient_checkpointing,
            learning_rate=args.lr,
            logging_steps=10,
            save_strategy=sched["save_strategy"],
            save_steps=sched["save_steps"],
            save_total_limit=sched["save_total_limit"],
            eval_strategy=sched["eval_strategy"],
            eval_steps=sched["eval_steps"],
            load_best_model_at_end=sched["load_best_model_at_end"],
            metric_for_best_model=sched["metric_for_best_model"],
            greater_is_better=sched["greater_is_better"],
            fp16=not torch.cuda.is_bf16_supported(),
            bf16=torch.cuda.is_bf16_supported(),
            optim="paged_adamw_32bit" if args.load_in_4bit else "adamw_torch",
            remove_unused_columns=False,
            max_length=args.max_length,
            completion_only_loss=True,
        )

        trainer = SFTTrainer(
            model=model,
            train_dataset=train_ds,
            eval_dataset=eval_ds,
            peft_config=peft_config,
            processing_class=tokenizer,
            args=training_args,
        )
    elif args.method == "dpo":
        logger.info("Initializing DPOTrainer")

        # DPOTrainer natively handles prompt, chosen, rejected fields. We only
        # need to verify the fields exist.
        for col in ["prompt_system", "prompt_user", "chosen", "rejected"]:
            if col not in train_ds.column_names and col == "prompt_system":
                train_ds = train_ds.rename_column("system", "prompt_system")
                if eval_ds:
                    eval_ds = eval_ds.rename_column("system", "prompt_system")
            if col not in train_ds.column_names and col == "prompt_user":
                train_ds = train_ds.rename_column("user", "prompt_user")
                if eval_ds:
                    eval_ds = eval_ds.rename_column("user", "prompt_user")

        # Map dataset to chat-templated prompt, chosen, rejected
        def map_dpo_fields(examples):
            prompts = []
            for sys_p, usr_p in zip(examples["prompt_system"], examples["prompt_user"], strict=False):
                prompts.append(render_serve_prompt(tokenizer, sys_p or "", usr_p or ""))
            return {
                "prompt": prompts,
                "chosen": examples["chosen"],
                "rejected": examples["rejected"],
            }

        dpo_train_ds = train_ds.map(map_dpo_fields, batched=True)
        dpo_eval_ds = eval_ds.map(map_dpo_fields, batched=True) if eval_ds else None

        training_args = DPOConfig(
            output_dir=str(args.output_dir),
            num_train_epochs=args.epochs,
            per_device_train_batch_size=args.batch_size,
            gradient_accumulation_steps=args.gradient_accumulation_steps,
            gradient_checkpointing=args.gradient_checkpointing,
            learning_rate=args.lr,
            logging_steps=10,
            save_strategy=sched["save_strategy"],
            save_steps=sched["save_steps"],
            save_total_limit=sched["save_total_limit"],
            eval_strategy=sched["eval_strategy"],
            eval_steps=sched["eval_steps"],
            load_best_model_at_end=sched["load_best_model_at_end"],
            metric_for_best_model=sched["metric_for_best_model"],
            greater_is_better=sched["greater_is_better"],
            fp16=not torch.cuda.is_bf16_supported(),
            bf16=torch.cuda.is_bf16_supported(),
            optim="paged_adamw_32bit" if args.load_in_4bit else "adamw_torch",
            remove_unused_columns=False,
            max_length=args.max_length,
            beta=0.1,
        )

        trainer = DPOTrainer(
            model=model,
            ref_model=None,  # DPOTrainer disables ref model internally when peft is used to save memory
            args=training_args,
            train_dataset=dpo_train_ds,
            eval_dataset=dpo_eval_ds,
            processing_class=tokenizer,
            peft_config=peft_config,
        )

    if sched["early_stopping"]:
        trainer.add_callback(
            EarlyStoppingCallback(
                early_stopping_patience=args.early_stopping_patience,
                early_stopping_threshold=args.early_stopping_threshold,
            )
        )
        logger.info(
            f"Early stopping armed: patience={args.early_stopping_patience} evals, "
            f"threshold={args.early_stopping_threshold}, best checkpoint restored at end."
        )

    logger.info("Starting training...")
    trainer.train()

    logger.info(f"Saving fine-tuned adapter model to {args.output_dir}")
    trainer.model.save_pretrained(str(args.output_dir))
    tokenizer.save_pretrained(str(args.output_dir))

    dest_dir = Path("/home/joshu/.cache/adapters") / args.output_dir.name
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        import shutil

        shutil.copytree(args.output_dir, dest_dir, dirs_exist_ok=True)
        logger.info(f"Copied adapter to {dest_dir}")
    except Exception as e:
        logger.warning(f"Could not copy adapter to {dest_dir}: {e}")

    logger.info("Training completed successfully!")


if __name__ == "__main__":
    main()
