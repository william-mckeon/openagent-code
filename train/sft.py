"""
train/sft.py

Stage 5 of the distillation flywheel (specs/0005) — THE trainer. The one real code build
in the plan: turn the curated per-step rows (`train/dataset/sft.jsonl`, produced by
`train/convert.py`) into a student checkpoint by LoRA-SFT.

Run:
  # smoke: tiny model + a handful of steps, proves the pipeline end-to-end (even CPU)
  python -m train.sft --smoke

  # real Tier-1 run on a GPU box
  python -m train.sft --model openai/gpt-oss-20b --epochs 1 --out train/checkpoints/student

What it does:
  1. load_rows      — read the SFT rows (each = {messages, completion, tools, meta}).
  2. build_example  — THE DATA BRIDGE: render messages+completion through the tokenizer's
     chat template (with the row's tools), then MASK the prompt so loss is on the agent's
     ACTION only (completion-only SFT — we clone the decisions, not the user/tool text).
  3. LoRA-SFT       — load the student (optionally 4-bit), attach a LoRA adapter, train.
  4. save           — write the adapter + tokenizer to --out.

The heavy deps (torch/transformers/peft/datasets) are imported LAZILY inside main(), so this
file imports and syntax-checks without the `[train]` extra. Install it on the GPU box:
  pip install -e ".[train]"   (+ a CUDA torch build for that machine)

Serving the result (Stage 6): merge the adapter, serve on vLLM, and swap it in behind the
existing CODE_API_BASE one-liner — see train/README.md.
"""
import os
import sys
import json
import argparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DEFAULT_DATA = os.path.join(ROOT, "train", "dataset", "sft.jsonl")
DEFAULT_OUT = os.path.join(ROOT, "train", "checkpoints", "student")
# A tiny instruct model with a real chat template — small enough to smoke-test the loop
# anywhere (CPU in a pinch). The real run points --model at gpt-oss-20b (Tier 1).
SMOKE_MODEL = "Qwen/Qwen2.5-0.5B-Instruct"


# ---------------------------------------------------------------- data bridge

def load_rows(path):
    """Read the per-step SFT rows convert.py wrote (one JSON object per line)."""
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _as_assistant(completion):
    """Normalize a row's `completion` into an assistant chat message."""
    msg = {"role": "assistant", "content": completion.get("content") or ""}
    if completion.get("tool_calls"):
        msg["tool_calls"] = completion["tool_calls"]
    return msg


def _flatten(messages):
    """Fallback text rendering for tokenizers whose chat template can't handle tool_calls —
    keeps the smoke path working on ANY model. Loses native tool formatting (fine for a
    plumbing proof; the real gpt-oss run uses the harmony template path above)."""
    out = []
    for m in messages:
        role = m.get("role", "?")
        content = m.get("content") or ""
        tcs = m.get("tool_calls") or []
        if tcs:
            calls = "; ".join(f"{t['function']['name']}({t['function']['arguments']})" for t in tcs)
            content = (content + "\n" if content else "") + f"[tool_calls] {calls}"
        out.append(f"{role}: {content}")
    return "\n".join(out)


def build_example(tokenizer, row, max_len):
    """THE BRIDGE: one row -> {input_ids, labels, attention_mask} with the PROMPT masked
    (-100) so loss falls only on the agent's action. Returns None if there's nothing to
    train on (empty completion)."""
    messages = row.get("messages") or []
    tools = row.get("tools") or None
    completion = _as_assistant(row.get("completion") or {})

    try:
        prompt_ids = tokenizer.apply_chat_template(
            messages, tools=tools, add_generation_prompt=True, tokenize=True)
        full_ids = tokenizer.apply_chat_template(
            messages + [completion], tools=tools, add_generation_prompt=False, tokenize=True)
    except Exception:
        # Template can't render tool_calls -> flat-text fallback.
        prompt_text = _flatten(messages) + "\nassistant: "
        target_text = (completion.get("content") or "")
        if completion.get("tool_calls"):
            calls = "; ".join(f"{t['function']['name']}({t['function']['arguments']})"
                              for t in completion["tool_calls"])
            target_text = (target_text + "\n" if target_text else "") + f"[tool_calls] {calls}"
        prompt_ids = tokenizer(prompt_text)["input_ids"]
        eos = [tokenizer.eos_token_id] if tokenizer.eos_token_id is not None else []
        full_ids = prompt_ids + tokenizer(target_text, add_special_tokens=False)["input_ids"] + eos

    n = len(prompt_ids)
    if full_ids[:n] != prompt_ids:          # template quirk: not an exact prefix -> be lenient
        n = min(n, len(full_ids))
    if n >= len(full_ids):                  # nothing to learn (empty action)
        return None
    input_ids = full_ids[:max_len]
    labels = ([-100] * n + full_ids[n:])[:max_len]
    return {"input_ids": input_ids, "attention_mask": [1] * len(input_ids), "labels": labels}


# ---------------------------------------------------------------- training

def main(argv=None):
    ap = argparse.ArgumentParser(description="LoRA-SFT a student on captured agent trajectories.")
    ap.add_argument("--model", default=None, help="HF model id of the student (default: smoke model)")
    ap.add_argument("--data", default=DEFAULT_DATA, help="path to sft.jsonl")
    ap.add_argument("--out", default=DEFAULT_OUT, help="output dir for the adapter + tokenizer")
    ap.add_argument("--epochs", type=float, default=1.0)
    ap.add_argument("--lr", type=float, default=2e-4)
    ap.add_argument("--batch", type=int, default=1)
    ap.add_argument("--grad-accum", type=int, default=8)
    ap.add_argument("--max-len", type=int, default=4096)
    ap.add_argument("--load-4bit", action="store_true", help="4-bit base (bitsandbytes; needs CUDA)")
    ap.add_argument("--smoke", action="store_true",
                    help="tiny model + few steps + few rows: prove the pipeline anywhere")
    args = ap.parse_args(argv)

    model_id = args.model or (SMOKE_MODEL if args.smoke else None)
    if not model_id:
        print("Specify --model <hf-id> (or --smoke for the tiny default).")
        return 2

    # Lazy imports — keep this module importable without the [train] extra.
    try:
        import torch
        from datasets import Dataset
        from transformers import (AutoTokenizer, AutoModelForCausalLM, Trainer,
                                   TrainingArguments, DataCollatorForSeq2Seq)
        from peft import LoraConfig, get_peft_model
    except ImportError as e:
        print(f"Missing a training dependency ({e.name}). Install the extra on your GPU box:\n"
              "  pip install -e \".[train]\"   (plus a CUDA torch build)")
        return 1

    if not os.path.isfile(args.data):
        print(f"No dataset at {args.data} — run `python -m train.capture` then `python -m train.convert` first.")
        return 1

    print(f"sft | model={model_id} | data={os.path.relpath(args.data, ROOT)} | "
          f"{'SMOKE' if args.smoke else 'full'}")

    tokenizer = AutoTokenizer.from_pretrained(model_id)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    rows = load_rows(args.data)
    if args.smoke:
        rows = rows[:64]
    examples = [e for e in (build_example(tokenizer, r, args.max_len) for r in rows) if e]
    if not examples:
        print("No trainable examples produced from the dataset.")
        return 1
    print(f"rows={len(rows)} -> trainable_examples={len(examples)}")
    dataset = Dataset.from_list(examples)

    model_kwargs = {}
    if args.load_4bit:
        from transformers import BitsAndBytesConfig
        model_kwargs["quantization_config"] = BitsAndBytesConfig(
            load_in_4bit=True, bnb_4bit_quant_type="nf4",
            bnb_4bit_compute_dtype=torch.bfloat16, bnb_4bit_use_double_quant=True)
        model_kwargs["device_map"] = "auto"
    model = AutoModelForCausalLM.from_pretrained(model_id, **model_kwargs)
    model.config.use_cache = False

    lora = LoraConfig(
        r=8 if args.smoke else 16, lora_alpha=16 if args.smoke else 32, lora_dropout=0.05,
        bias="none", task_type="CAUSAL_LM",
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj"])
    model = get_peft_model(model, lora)
    model.print_trainable_parameters()

    targs = TrainingArguments(
        output_dir=args.out, per_device_train_batch_size=args.batch,
        gradient_accumulation_steps=args.grad_accum, learning_rate=args.lr,
        num_train_epochs=args.epochs, max_steps=5 if args.smoke else -1,
        logging_steps=1, save_strategy="no", report_to=[],
        bf16=torch.cuda.is_available(), warmup_ratio=0.03, lr_scheduler_type="cosine")
    trainer = Trainer(
        model=model, args=targs, train_dataset=dataset,
        data_collator=DataCollatorForSeq2Seq(tokenizer, padding=True, label_pad_token_id=-100))

    trainer.train()
    os.makedirs(args.out, exist_ok=True)
    model.save_pretrained(args.out)
    tokenizer.save_pretrained(args.out)
    print(f"\nsaved LoRA adapter + tokenizer -> {os.path.relpath(args.out, ROOT)}")
    print("next: merge + serve on vLLM, then swap in via CODE_API_BASE (Stage 6; see train/README.md)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
