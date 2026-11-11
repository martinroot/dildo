#!/usr/bin/env python3
"""Run VelvetFlux FineTune jobs from a private local queue (Linux)."""
import argparse
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
from tempfile import NamedTemporaryFile
import time

TRAINER = Path(__file__).with_name("trainer.py")
DEFAULT_ROOT = "/var/lib/ft-node"
JOB_ID = re.compile(r"[0-9]{14}-[a-f0-9]{12}")
PRESETS = {"smoke": (12, 1, 4), "standard": (160, 4, 16)}
TERMINAL = {"completed", "failed", "cancelled"}
stopping = False


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path, data):
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        try:
            json.dump(data, handle, ensure_ascii=False, allow_nan=False)
            handle.write("\n")
            handle.close()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def read_json(path):
    try:
        with path.open(encoding="utf-8") as handle:
            value = json.load(handle)
    except FileNotFoundError:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object in {path.name}")
    return value


def stop(_signum, _frame):
    global stopping
    stopping = True


def status(directory, state, phase, message=None):
    payload = {"state": state, "phase": phase, "updated_at": utc_now()}
    if message is not None:
        payload["message"] = message
    atomic_json(directory / "status.json", payload)


def heartbeat(root, job=None):
    atomic_json(root / "worker.json", {"updated_at": utc_now(), "job": job, "pid": os.getpid(), "hostname": __import__("socket").gethostname()})


def terminate(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def run_job(root, directory, model, threads, timeout):
    meta = read_json(directory / "job.json")
    if meta.get("id") != directory.name:
        raise ValueError("Job ID mismatch")
    dataset = meta.get("dataset")
    if not isinstance(dataset, str) or not JOB_ID.fullmatch(dataset):
        raise ValueError("Invalid dataset ID")
    device = meta.get("device")
    if device not in ("cpu", "cuda") or meta.get("preset") not in PRESETS:
        raise ValueError("Invalid device or preset")
    if (directory / "cancel.json").is_file():
        status(directory, "cancelled", "cancelled")
        return

    dataset_dir = root / os.environ.get("FT_DATA_SUBDIR", "datasets") / dataset
    result = directory / "result"
    steps, accumulate, samples = PRESETS[meta["preset"]]
    command = [
        sys.executable, str(TRAINER), "train",
        "--model", model, "--device", device,
        "--train", str(dataset_dir / "train.jsonl"), "--test", str(dataset_dir / "test.jsonl"),
        "--out", str(result), "--progress-file", str(directory / "progress.json"),
        "--steps", str(steps), "--accumulate", str(accumulate), "--eval-examples", str(samples),
    ]
    env = {
        **os.environ, "OMP_NUM_THREADS": str(threads), "MKL_NUM_THREADS": str(threads),
        "TOKENIZERS_PARALLELISM": "false", "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1",
    }
    status(directory, "running", "starting")
    deadline = time.monotonic() + timeout
    with (directory / "worker.log").open("ab") as log:
        process = subprocess.Popen(command, env=env, stdout=log, stderr=subprocess.STDOUT)
        try:
            while process.poll() is None:
                heartbeat(root, directory.name)
                if stopping or (directory / "cancel.json").is_file():
                    terminate(process)
                    status(directory, "cancelled", "cancelled", "Обучение остановлено.")
                    return
                if time.monotonic() >= deadline:
                    terminate(process)
                    status(directory, "failed", "timeout", "Превышено время задания.")
                    return
                time.sleep(2)
        finally:
            terminate(process)

    if process.returncode == 0 and (result / "adapter/adapter_model.safetensors").is_file() and read_json(result / "report.json"):
        status(directory, "completed", "done")
    else:
        status(directory, "failed", "error", "Ошибка тренера. Подробности в журнале запуска.")


def run_queue(root, model, threads, timeout, once, executor="local"):
    with (root / "worker.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("A FineTune worker already holds the queue lock", file=sys.stderr)
            return 1
        for directory in sorted((root / "jobs").iterdir()):
            if JOB_ID.fullmatch(directory.name) and (directory / "job.json").is_file():
                if read_json(directory / "job.json").get("executor", "local") == executor and read_json(directory / "status.json").get("state") == "running":
                    status(directory, "failed", "interrupted", "Предыдущий воркер завершился до конца обучения.")
        try:
            while not stopping:
                heartbeat(root)
                pending = next((
                    directory for directory in sorted((root / "jobs").iterdir())
                    if JOB_ID.fullmatch(directory.name) and (directory / "job.json").is_file()
                    and read_json(directory / "job.json").get("executor", "local") == executor
                    and read_json(directory / "status.json").get("state") not in TERMINAL
                ), None)
                if pending is not None:
                    try:
                        run_job(root, pending, model, threads, timeout)
                    except (OSError, ValueError) as error:
                        print(f"{pending.name}: {error}", file=sys.stderr)
                        status(pending, "failed", "error", "Воркер не смог обработать задание.")
                if once:
                    return 0
                if pending is None:
                    time.sleep(3)
        finally:
            (root / "worker.json").unlink(missing_ok=True)
    return 0


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(os.getenv("RTB_FINETUNE_DIR", DEFAULT_ROOT)))
    parser.add_argument("--model", default=os.getenv("RTB_FINETUNE_MODEL", "Qwen/Qwen3.5-0.8B-Base"))
    parser.add_argument("--threads", type=int, default=2)
    parser.add_argument("--timeout", type=int, default=14400, help="job timeout in seconds")
    parser.add_argument("--executor", choices=["local", "vast"], default="local")
    parser.add_argument("--once", action="store_true", help="handle at most one queued job and exit")
    parser.add_argument("--predict", type=Path, help="use the active adapter for snapshots JSONL")
    args = parser.parse_args()
    if args.threads < 1 or args.timeout < 1:
        parser.error("threads and timeout must be positive")
    root = args.root.resolve()
    if args.predict is not None:
        active = read_json(root / "active.json").get("active")
        if not isinstance(active, str) or not JOB_ID.fullmatch(active):
            parser.error("no active adapter")
        job = root / "jobs" / active
        if read_json(job / "status.json").get("state") != "completed":
            parser.error("active adapter is unavailable")
        return subprocess.call([
            sys.executable, str(TRAINER), "predict",
            "--adapter", str(job / "result/adapter"), "--input", str(args.predict.resolve()),
        ])
    if not (root / "jobs").is_dir() or not (root / os.environ.get("FT_DATA_SUBDIR", "datasets")).is_dir():
        parser.error("queue root must contain jobs and dataset directories")
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    return run_queue(root, args.model, args.threads, args.timeout, args.once, args.executor)


if __name__ == "__main__":
    raise SystemExit(main())
