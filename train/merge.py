"""
train/merge.py

Stage 6 — fold a trained LoRA adapter back into its base model, producing a STANDALONE
model directory a server can load directly. (A LoRA checkpoint is just the small adapter;
vLLM/serving wants the full merged weights — or a base it can attach the adapter to. Merging
is the simplest, most portable form.)

Run:
  python -m train.merge --adapter train/checkpoints/student
  # -> train/checkpoints/student-merged   (then serve that; see train/README.md)

The base model id is read from the adapter's own adapter_config.json, so usually you only
pass --adapter. Heavy deps are lazy-imported (this file imports without the [train] extra).
"""
import os
import sys
import json
import argparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Merge a LoRA adapter into its base model.")
    ap.add_argument("--adapter", default="train/checkpoints/student",
                    help="the LoRA adapter dir (output of train/sft.py)")
    ap.add_argument("--base", default=None,
                    help="base model id (default: read from the adapter's adapter_config.json)")
    ap.add_argument("--out", default=None,
                    help="output dir for the merged model (default: <adapter>-merged)")
    args = ap.parse_args(argv)

    adapter = args.adapter if os.path.isabs(args.adapter) else os.path.join(ROOT, args.adapter)
    cfg = os.path.join(adapter, "adapter_config.json")
    if not os.path.isfile(cfg):
        print(f"No adapter_config.json in {args.adapter} — is that a LoRA adapter dir? "
              "(run train/sft.py first)")
        return 1
    base = args.base or json.load(open(cfg, encoding="utf-8")).get("base_model_name_or_path")
    if not base:
        print("Could not determine the base model — pass --base <hf-id>.")
        return 1
    out = args.out or (adapter.rstrip("/\\") + "-merged")

    try:
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer
        from peft import PeftModel
    except ImportError as e:
        print(f"Missing a training dependency ({e.name}). Install the extra on your GPU box:\n"
              "  pip install -e \".[train]\"   (or run inside the docker/train image)")
        return 1

    print(f"merge | base={base} | adapter={args.adapter} -> {os.path.relpath(out, ROOT)}")
    model = AutoModelForCausalLM.from_pretrained(base, torch_dtype=torch.float16)
    model = PeftModel.from_pretrained(model, adapter)
    model = model.merge_and_unload()           # bake the adapter into the base weights
    os.makedirs(out, exist_ok=True)
    model.save_pretrained(out, safe_serialization=True)
    AutoTokenizer.from_pretrained(adapter).save_pretrained(out)
    print(f"merged model + tokenizer saved -> {os.path.relpath(out, ROOT)}")
    print("next: serve it (docker compose up serve) and point CODE_API_BASE at it (Stage 6).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
