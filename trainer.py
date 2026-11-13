#!/usr/bin/env python3
"""Supervised LoRA for snapshot-to-action JSON."""
import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import random
import re
import sys

BASE = "Qwen/Qwen3.5-0.8B-Base"
START, END = "<|im_start|>", "<|im_end|>"
REASONS = {
    "hold": {"pending", "low_sample", "borderline"},
    "pause": {"loss"},
    "scale": {"profit"},
}
SYSTEM = (
    "You are a traffic tracking policy. Return only a JSON array. "
    "Return one object per input slice, with exactly slice_id, action and reason. "
    "Copy slice_id from the input. Valid action/reason pairs are "
    "hold/pending, hold/low_sample, hold/borderline, pause/loss, scale/profit."
)
SYSTEM_TEXT = (
    "You are a precise assistant. Follow the task instructions in the user "
    "message exactly and output only the requested answer."
)
PROJECTIONS = {
    "q_proj", "k_proj", "v_proj", "o_proj",
    "in_proj_qkv", "in_proj_z", "in_proj_a", "in_proj_b", "out_proj",
    "gate_proj", "up_proj", "down_proj",
}


def compact(value):
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), allow_nan=False)


def read_jsonl(path):
    items = []
    with Path(path).open(encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            if line.strip():
                try:
                    items.append(json.loads(line))
                except ValueError as exc:
                    raise ValueError(f"{path}:{line_no}: {exc}") from exc
    if not items:
        raise ValueError(f"{path}: expected at least one JSONL row")
    return items


def write_jsonl(path, items):
    with Path(path).open("w", encoding="utf-8") as handle:
        for item in items:
            handle.write(compact(item) + "\n")


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError("as_of must be an ISO-8601 timestamp with a timezone")
    result = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    if result.utcoffset() is None:
        raise ValueError("as_of must include a timezone")
    return result


def validate_snapshot(snapshot):
    if not isinstance(snapshot, dict) or set(snapshot) != {"as_of", "slices"}:
        raise ValueError("snapshot must have as_of and slices")
    timestamp(snapshot["as_of"])
    slices = snapshot["slices"]
    if not isinstance(slices, list) or not 1 <= len(slices) <= 3:
        raise ValueError("expected 1–3 slices per snapshot")
    ids = set()
    for row in slices:
        fields = {"slice_id", "clicks", "conversions", "profit_cents", "postbacks_mature"}
        if not isinstance(row, dict) or set(row) != fields:
            raise ValueError(f"slice fields must be {sorted(fields)}")
        slice_id = row["slice_id"]
        if not isinstance(slice_id, str) or not re.fullmatch(r"[A-Za-z0-9:_-]{1,32}", slice_id):
            raise ValueError("invalid slice_id")
        if slice_id in ids:
            raise ValueError(f"duplicate slice_id: {slice_id}")
        ids.add(slice_id)
        for field in ("clicks", "conversions", "profit_cents"):
            if type(row[field]) is not int:
                raise ValueError(f"{field} must be an integer")
        if min(row["clicks"], row["conversions"]) < 0:
            raise ValueError("clicks and conversions must be non-negative")
        if type(row["postbacks_mature"]) is not bool:
            raise ValueError("postbacks_mature must be a boolean")
    return ids


def validate_actions(actions, expected_ids):
    if not isinstance(actions, list) or len(actions) != len(expected_ids):
        raise ValueError("expected an array with exactly one action per slice")
    seen = set()
    for row in actions:
        if not isinstance(row, dict) or set(row) != {"slice_id", "action", "reason"}:
            raise ValueError("action fields must be slice_id, action, reason")
        slice_id, action, reason = row["slice_id"], row["action"], row["reason"]
        if not all(isinstance(value, str) for value in (slice_id, action, reason)):
            raise ValueError("action fields must be strings")
        if slice_id not in expected_ids or slice_id in seen:
            raise ValueError(f"unexpected or repeated slice_id: {slice_id}")
        if reason not in REASONS.get(action, ()):
            raise ValueError(f"invalid action/reason pair: {action}/{reason}")
        seen.add(slice_id)


def label(snapshot):
    validate_snapshot(snapshot)
    output = []
    for row in snapshot["slices"]:
        if not row["postbacks_mature"]:
            action, reason = "hold", "pending"
        elif row["clicks"] < 200:
            action, reason = "hold", "low_sample"
        elif row["profit_cents"] <= -25:
            action, reason = "pause", "loss"
        elif row["profit_cents"] >= 25 and row["conversions"] >= 3:
            action, reason = "scale", "profit"
        else:
            action, reason = "hold", "borderline"
        output.append({"slice_id": row["slice_id"], "action": action, "reason": reason})
    return {"input": snapshot, "output": output}


def validate_pair(pair):
    if not isinstance(pair, dict) or set(pair) != {"input", "output"}:
        raise ValueError("expected input and output")
    if isinstance(pair["input"], str):
        if not pair["input"].strip():
            raise ValueError("input text must be non-empty")
        if not isinstance(pair["output"], str) or not pair["output"].strip():
            raise ValueError("output text must be a non-empty string")
        return "text"
    validate_actions(pair["output"], validate_snapshot(pair["input"]))
    return "slices"


def load_pairs(path):
    items = read_jsonl(path)
    schemas = set()
    for index, pair in enumerate(items, 1):
        try:
            schemas.add(validate_pair(pair))
        except ValueError as exc:
            raise ValueError(f"{path}: record {index}: {exc}") from exc
    if len(schemas) > 1:
        raise ValueError(f"{path}: mixed schemas in one dataset: {sorted(schemas)}")
    return items


def demo(directory):
    directory.mkdir(parents=True, exist_ok=True)
    rng = random.Random(2026)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    all_pairs = []
    for hour in range(560):
        countries = rng.sample(("BR", "MX", "AR", "ID", "TH", "VN", "PH", "CO"), rng.randint(1, 3))
        slices = []
        for geo in countries:
            clicks = rng.randint(60, 1000)
            conversions = rng.randint(0, max(1, clicks // 55))
            cost_cents = round(clicks * rng.uniform(0.03, 0.18))
            revenue_cents = conversions * rng.randint(8, 35) + rng.randint(0, 25)
            slices.append({
                "slice_id": f"{rng.choice((1290, 1291, 1292))}:{geo}",
                "clicks": clicks,
                "conversions": conversions,
                "profit_cents": revenue_cents - cost_cents,
                "postbacks_mature": rng.random() >= 0.15,
            })
        all_pairs.append(label({"as_of": (start + timedelta(hours=hour)).isoformat(), "slices": slices}))
    write_jsonl(directory / "train.jsonl", all_pairs[:512])
    write_jsonl(directory / "test.jsonl", all_pairs[512:])
    write_jsonl(directory / "input.jsonl", [all_pairs[512]["input"]])
    print(f"{directory}: 512 train, 48 test, one example input")


def prompt_ids(tokenizer, snapshot):
    user = compact(snapshot) if not isinstance(snapshot, str) else snapshot
    system = SYSTEM if not isinstance(snapshot, str) else SYSTEM_TEXT
    prompt = (
        f"{START}system\n{system}{END}\n"
        f"{START}user\n{user}{END}\n{START}assistant\n"
    )
    return tokenizer.encode(prompt, add_special_tokens=False)


def encode_pairs(tokenizer, pairs, max_length):
    result = []
    for pair in pairs:
        prefix = prompt_ids(tokenizer, pair["input"])
        tail = pair["output"] if isinstance(pair["output"], str) else compact(pair["output"])
        completion = tokenizer.encode(tail + END, add_special_tokens=False)
        input_ids = prefix + completion
        if len(input_ids) > max_length:
            raise ValueError(
                f"sample at {pair['input']['as_of']} uses {len(input_ids)} tokens "
                f"(limit {max_length}); shorten the input or raise --max-length"
            )
        result.append({"input_ids": input_ids, "labels": [-100] * len(prefix) + completion})
    return result


def load_model(model_id, device="auto"):
    import os
    import torch
    from transformers import AutoTokenizer, Qwen3_5ForConditionalGeneration
    # Prefer the pre-downloaded local base checkpoint to avoid repeat HF downloads.
    local = os.environ.get("RTB_FINETUNE_MODEL")
    if local and Path(local, "config.json").is_file():
        model_id = local

    if device == "auto":
        device = "cuda" if torch.cuda.is_available() else "cpu"
    if device == "cuda" and not torch.cuda.is_available():
        raise ValueError("CUDA was requested but is unavailable")
    # CPU: use bfloat16 weights too (halves RAM; supported by Qwen3.5 linear layers)
    bf16 = (device == "cuda" and torch.cuda.is_bf16_supported()) or device == "cpu"
    dtype = torch.bfloat16 if bf16 else torch.float32
    tokenizer = AutoTokenizer.from_pretrained(model_id)
    for marker in (START, END):
        token_id = tokenizer.convert_tokens_to_ids(marker)
        if token_id is None or tokenizer.encode(marker, add_special_tokens=False) != [token_id]:
            raise ValueError(f"checkpoint must support Qwen chat token {marker}")
    model = Qwen3_5ForConditionalGeneration.from_pretrained(model_id, dtype=dtype)
    return model.to(device), tokenizer, bf16


def generate_text(model, tokenizer, text_input, max_new_tokens, capture=None):
    import torch

    encoded = torch.tensor([prompt_ids(tokenizer, text_input)], device=model.device)
    stop_id = tokenizer.convert_tokens_to_ids(END)
    pad_id = tokenizer.pad_token_id if tokenizer.pad_token_id is not None else stop_id
    with torch.inference_mode():
        generated = model.generate(
            input_ids=encoded,
            attention_mask=torch.ones_like(encoded),
            do_sample=False,
            max_new_tokens=max_new_tokens,
            eos_token_id=stop_id,
            pad_token_id=pad_id,
        )
    text = tokenizer.decode(
        generated[0, encoded.shape[-1]:], skip_special_tokens=True, clean_up_tokenization_spaces=False
    ).strip()
    if capture is not None:
        capture["raw_output"] = text
    return text


def token_f1(predicted, reference):
    from collections import Counter

    p = re.findall(r"[a-z0-9]+", predicted.lower())
    r = re.findall(r"[a-z0-9]+", reference.lower())
    if not p or not r:
        return 0.0
    overlap = sum((Counter(p) & Counter(r)).values())
    precision, recall = overlap / len(p), overlap / len(r)
    return 2 * precision * recall / (precision + recall) if precision + recall else 0.0


def generate_actions(model, tokenizer, snapshot, max_new_tokens, capture=None):
    text = generate_text(model, tokenizer, snapshot, max_new_tokens, capture)
    actions = json.loads(text)
    validate_actions(actions, {row["slice_id"] for row in snapshot["slices"]})
    return actions


def score_text_pair(model, tokenizer, pair, max_new_tokens, captured):
    """Score a free-text pair. Returns (valid, exact, correct_fields, total_fields, f1_sum)."""
    expected = pair["output"]
    try:
        expected_json = json.loads(expected)
    except ValueError:
        expected_json = None
    text = generate_text(model, tokenizer, pair["input"], max_new_tokens, captured)
    actual_json = None
    if expected_json is not None:
        try:
            actual_json = json.loads(text)
        except ValueError:
            actual_json = None
    if expected_json is None:
        f1 = token_f1(text, expected)
        exact = text.strip() == expected.strip()
        correct, total = int(f1 >= 0.5), 1
        return bool(text.strip()), exact, correct, total, f1
    if not isinstance(actual_json, dict) or set(actual_json) != set(expected_json):
        return False, False, 0, len(expected_json), 0.0
    correct = total = 0
    f1_sum = 0.0
    structured_exact = True
    for key, value in expected_json.items():
        if key == "reply" or not isinstance(value, str) or len(value) > 100:
            f1_sum += token_f1(str(actual_json.get(key, "")), str(value))
            continue
        total += 1
        if actual_json.get(key) == value:
            correct += 1
        else:
            structured_exact = False
    return True, structured_exact, correct, total, f1_sum


def score(model, tokenizer, pairs, max_new_tokens, progress):
    valid = exact = correct_rows = total_rows = 0
    f1_sum = 0.0
    model.eval()
    samples = []
    for index, pair in enumerate(pairs, 1):
        progress(index - 1, len(pairs))
        captured = {}
        if isinstance(pair["input"], str):
            ok, exact_i, correct_i, total_i, f1_i = score_text_pair(
                model, tokenizer, pair, max_new_tokens, captured)
            valid += int(ok); exact += int(exact_i)
            correct_rows += correct_i; total_rows += total_i
            f1_sum += f1_i
            if index <= 100:
                samples.append({"input": pair["input"], "expected": pair["output"], **captured})
            continue
        expected = {row["slice_id"]: row for row in pair["output"]}
        total_rows += len(expected)
        try:
            actions = generate_actions(model, tokenizer, pair["input"], max_new_tokens, captured)
        except ValueError:
            if index <= 100:
                samples.append({"input": pair["input"], "expected": pair["output"], "output": None, **captured})
            continue
        if index <= 100:
            samples.append({"input": pair["input"], "expected": pair["output"], "output": actions, **captured})
        valid += 1
        matches = sum(row == expected[row["slice_id"]] for row in actions)
        correct_rows += matches
        exact += matches == len(expected)
    report = {
        "examples": len(pairs),
        "valid_json_and_schema": valid,
        "exact_arrays": exact,
        "exact_slices": correct_rows,
        "total_slices": total_rows,
        "samples": samples,
    }
    if valid:
        report["mean_token_f1"] = round(f1_sum / valid, 4)
    return report


def train(args):
    from worker import atomic_json, utc_now

    def progress(phase, current=None, total=None):
        if args.progress_file:
            atomic_json(args.progress_file, {
                "phase": phase, "current": current, "total": total, "updated_at": utc_now(),
            })

    progress("validating")
    train_pairs, test_pairs = load_pairs(args.train), load_pairs(args.test)
    if not isinstance(train_pairs[0]["input"], str):
        last_train = max(timestamp(row["input"]["as_of"]) for row in train_pairs)
        first_test = min(timestamp(row["input"]["as_of"]) for row in test_pairs)
        if last_train >= first_test:
            raise ValueError("the entire test set must be later than the training set")
    if args.out.exists():
        raise ValueError(f"{args.out} already exists; choose a fresh output directory")

    import torch
    from peft import LoraConfig, TaskType, get_peft_model
    from transformers import Trainer, TrainerCallback, TrainingArguments, set_seed

    set_seed(2026)
    progress("loading_model")
    model, tokenizer, bf16 = load_model(args.model, args.device)
    encoded_train = encode_pairs(tokenizer, train_pairs, args.max_length)
    encode_pairs(tokenizer, test_pairs, args.max_length)
    targets = [
        name for name, module in model.named_modules()
        if name.startswith("model.language_model.layers.")
        and name.rsplit(".", 1)[-1] in PROJECTIONS
        and isinstance(module, torch.nn.Linear)
    ]
    if not targets:
        raise ValueError("no Qwen3.5 text decoder projections matched")
    model = get_peft_model(model, LoraConfig(
        task_type=TaskType.CAUSAL_LM, r=args.rank, lora_alpha=2 * args.rank,
        lora_dropout=0.0, target_modules=targets,
    ), autocast_adapter_dtype=False)  # torch 2.6 images lack float8_e8m0fnu
    model.print_trainable_parameters()
    def collate(batch):
        (row,) = batch  # One example per device, with gradient accumulation.
        input_ids = torch.tensor([row["input_ids"]])
        # Retain the final prompt position: its logit predicts the first JSON token.
        logits_to_keep = 1 + sum(label != -100 for label in row["labels"])
        return {
            "input_ids": input_ids,
            "attention_mask": torch.ones_like(input_ids),
            "labels": torch.tensor([row["labels"][-logits_to_keep:]]),
            "logits_to_keep": logits_to_keep,
            "use_cache": False,
        }

    class ProgressCallback(TrainerCallback):
        def on_log(self, training_args, state, control, logs=None, **kwargs):
            if logs and args.progress_file:
                point = {"step": state.global_step, "at": utc_now(), **{k: v for k, v in logs.items() if isinstance(v, (int, float))}}
                with args.progress_file.with_name("metrics.jsonl").open("a") as handle:
                    handle.write(json.dumps(point) + "\n")

        def on_step_end(self, training_args, state, control, **kwargs):
            progress("training", state.global_step, state.max_steps)

    trainer = Trainer(
        model=model,
        args=TrainingArguments(
            output_dir=str(args.out), learning_rate=args.lr,
            max_steps=args.steps, per_device_train_batch_size=1,
            gradient_accumulation_steps=args.accumulate,
            use_cpu=model.device.type == "cpu",
            bf16=bf16, gradient_checkpointing=True,
            gradient_checkpointing_kwargs={"use_reentrant": False},
            dataloader_pin_memory=model.device.type == "cuda",
            logging_steps=1, save_strategy="no", report_to="none",
            remove_unused_columns=False, seed=2026,
        ),
        train_dataset=encoded_train,
        data_collator=collate,
        callbacks=[ProgressCallback()] if args.progress_file else [],
    )
    progress("training", 0, args.steps)
    trainer.train()
    progress("saving")
    adapter = args.out / "adapter"
    model.save_pretrained(adapter)
    tokenizer.save_pretrained(adapter)

    # Use the same held-out examples, prompt and decoding for both runs.
    probe = random.Random(2026).sample(test_pairs, min(args.eval_examples, len(test_pairs)))
    with model.disable_adapter():
        before = score(model, tokenizer, probe, args.max_new_tokens,
                       lambda current, total: progress("evaluating_base", current, total))
    after = score(model, tokenizer, probe, args.max_new_tokens,
                  lambda current, total: progress("evaluating_adapter", current, total))
    report = {"base": before, "lora": after, "model": args.model}
    (args.out / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    progress("done")
    print(json.dumps({key: {k: v for k, v in part.items() if k != "samples"}
                      for key, part in report.items() if key in ("base", "lora")}, indent=2))


def predict(args):
    from peft import PeftConfig, PeftModel

    snapshots = read_jsonl(args.input)
    for snapshot in snapshots:
        validate_pair(snapshot)
    config = PeftConfig.from_pretrained(args.adapter)
    model, tokenizer, _ = load_model(config.base_model_name_or_path)
    model = PeftModel.from_pretrained(model, args.adapter)
    model.eval()
    results = []
    for snapshot in snapshots:
        try:
            if isinstance(snapshot["input"], str):
                print(generate_text(model, tokenizer, snapshot["input"], args.max_new_tokens))
            else:
                print(compact(generate_actions(model, tokenizer, snapshot, args.max_new_tokens)))
        except ValueError as exc:
            raise ValueError(f"invalid model response: {exc}") from exc


def positive_int(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("expected an integer >= 1")
    return number


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    demo_parser = commands.add_parser("demo", help="create synthetic JSONL examples")
    demo_parser.add_argument("--dir", type=Path, default=Path("demo"))
    label_parser = commands.add_parser("label", help="build training pairs using the demo rule")
    label_parser.add_argument("--input", type=Path, required=True)
    label_parser.add_argument("--out", type=Path, required=True)
    train_parser = commands.add_parser("train", help="train LoRA and compare to the base model")
    train_parser.add_argument("--train", type=Path, required=True)
    train_parser.add_argument("--test", type=Path, required=True)
    train_parser.add_argument("--out", type=Path, required=True)
    train_parser.add_argument("--model", default=BASE)
    train_parser.add_argument("--device", choices=["auto", "cpu", "cuda"], default="auto")
    train_parser.add_argument("--progress-file", type=Path)
    train_parser.add_argument("--rank", type=positive_int, default=8)
    train_parser.add_argument("--lr", type=float, default=2e-4)
    train_parser.add_argument("--steps", type=positive_int, default=160)
    train_parser.add_argument("--accumulate", type=positive_int, default=4)
    train_parser.add_argument("--max-length", type=positive_int, default=1024)
    train_parser.add_argument("--eval-examples", type=positive_int, default=12)
    train_parser.add_argument("--max-new-tokens", type=positive_int, default=300)
    predict_parser = commands.add_parser("predict", help="emit one validated action array per input")
    predict_parser.add_argument("--adapter", type=Path, required=True)
    predict_parser.add_argument("--input", type=Path, required=True)
    predict_parser.add_argument("--max-new-tokens", type=positive_int, default=180)
    args = parser.parse_args()
    try:
        if args.command == "demo":
            demo(args.dir)
        elif args.command == "label":
            pairs = [label(snapshot) for snapshot in read_jsonl(args.input)]
            write_jsonl(args.out, pairs)
        elif args.command == "train":
            train(args)
        elif args.command == "predict":
            predict(args)
    except (ValueError, OSError) as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
