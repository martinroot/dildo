# DILDO MLL

# Finetune for everyone

**Qwen3.5 + vast.ai API key = a trained adapter in ~10 minutes, from $0.03.**

A working fine-tune platform powering **dildo.asia**: Python control plane + LoRA trainer + local worker + Vast.ai SSH executor + a single-page portal (no frontend build step, no CDN, no Node dependencies).

Facts measured on a real smoke run (RTX 3090 at $0.17/h on vast.ai):

| Fact | Value |
| --- | --- |
| Smoke run (12 steps) end-to-end | ~10 min · ~$0.03 |
| Standard run (160 steps) | ~30 min · $0.08–0.12 |
| Portable artifact | ~30 MB LoRA adapter ZIP |
| Compute | on-demand RTX 3090 / 4090 |
| Base model | Qwen3.5-0.8B |

## What it does

1. **Register** — quick login & password; sessions are HMAC-signed cookies (30 days), passwords hashed with PBKDF2 (200k iterations). Machine clients use a Bearer token instead.
2. **Enter a vast.ai API key** — on-demand RTX 3090/4090, rented per run and released automatically.
3. **Upload Excel / CSV / TXT** — converted to immutable JSONL dataset versions with schema, duplicate and train/test leakage checks; every run records the SHA-256 of the data it trained on.
4. **Define acceptance criteria** — minimum held-out examples, slice accuracy, valid-JSON rate, no-regression: enforced server-side before activation.
5. **Train** — one click rents a GPU, uploads data + trainer over SSH, streams live progress (phase, %, step, loss), evaluates base vs adapter and collects artifacts before releasing the instance.
6. **Download** — the adapter ZIP: `adapter_model.safetensors`, `adapter_config.json`, tokenizer, `report.json`, `job.json`. Loadable with PEFT anywhere.

After training, adapters live in a catalog with metrics and cost history; the active one can be probed **hot** (resident model server, ~8 s per prediction, up to 20 snapshots per request) or downloaded. Every run's cost (hourly rate × wall time), duration and outcome is recorded in a history table.

## Three concerns

| Area | Portal pages | What happens |
| --- | --- | --- |
| Data | LIVE (test panel), Advanced → Datasets / DataAPI | Import train/test JSONL, strict schema, duplicates, leakage, time separation, push-provider registry |
| Training | LIVE, LoRA Model, History | Queue, CPU/CUDA, automatic GPU lifecycle, per-step loss, progress, logs, cost tracking |
| Quality & release | Results, Advanced → Training criteria | Base/LoRA comparison, real response traces, server-enforced release gates, activation & download |

## Quick start (interface)

The control plane needs only Python 3.10+; torch is required for the trainer only.

```bash
FT_DIR="$PWD/local-data" python3 ft_node.py
```

Open http://127.0.0.1:8094/. Data routes answer `401` until you register a user (the first registration works right in the portal UI) or send a Bearer token.

## Server installation

Sources live in `/opt/ft-node`, data in `/var/lib/ft-node` (owner `ft-node:ft-node`, mode `0700`).

1. Create the `ft-node` system user and the data directory.
2. Copy the project to `/opt/ft-node`; create a venv: `python3 -m venv /opt/ft-node/.venv`.
3. For local training install PyTorch and `pip install -r requirements.txt` inside the venv. The portal and the remote Vast executor do not need torch.
4. Copy `deploy/ft-node.env.example` to `/etc/ft-node/ft-node.env` and set a real `FT_API_TOKEN` (file `root:ft-node`, `0640`).
5. Install `ft-node.service`, `ft-worker.service` and `ft-predict.service` (resident hot-testing server, keeps the base model in RAM) into `/etc/systemd/system`; `systemctl daemon-reload` and start them.
6. For local training place a Qwen3.5 checkpoint in `/opt/ft-node/models/base` (the unit sets `HF_HUB_OFFLINE=1` and memory limits).
7. Terminate HTTPS in nginx; template in `deploy/nginx-dildo.conf.example`. The app itself handles authentication — do not put Basic auth in front unless you also trust `X-FT-Authenticated` from a local proxy.

The API stays on loopback. Never set `FT_BIND=0.0.0.0` in production. External clients use `/api/*` with a Bearer token; browsers use register/login sessions.

## Vast.ai executor

- The Vast API key stays on the controller (`/etc/ft-node/vast_api_key` or `VAST_API_KEY`); it is never sent to the GPU.
- Private SSH key: `/etc/ft-node/id_ed25519` (`FT_SSH_KEY`); add its public half to your Vast.ai account. Owner `ft-node`, mode `0600`.
- GPU image or SSH-compatible template hash is configured in the portal (default: PyTorch CUDA image). `FT_REMOTE_MODEL` selects the training checkpoint. Gated Hugging Face models and HF token delivery are not supported.
- SSH uses TOFU (`accept-new`); `known_hosts` is kept in `FT_DIR`.

**Debug flow:** Search marketplace → Provision → Health check → Release instance. Search is free; a provisioned debug instance bills until released.

**Automatic flow:** pick dataset + preset → Launch automatic run.

1. The dataset is re-validated and a private copy is snapshotted for the job.
2. An offer is found within GPU / VRAM / price / reliability limits.
3. Exactly one tracked instance is created; its ID and unique label are persisted before readiness is awaited.
4. Trainer, worker, dependencies, JSONL splits and metadata are uploaded over SSH.
5. The remote worker trains and evaluates base and adapter on identical held-out examples.
6. The controller collects status / progress / metrics / logs, then the full `result/` with the adapter weights.
7. The tracked instance is deleted on success, on ordinary errors, on cancellation and on timeout.

The time limit includes provisioning / install / training and is enforced by the controller; network timeouts and cleanup add latency, so it is not a hard provider-side billing cap. Storage/transfer are billed separately. If the controller dies mid-run (SIGKILL, host crash, Vast API outage), cleanup is not guaranteed: check the saved instance ID and the Vast console, then Sync / Release manually. There is no automatic re-rent after an ambiguous network failure — check the console first so a second lease is not created. Deletion is limited to the exact saved ID and unique label; there is no prefix-based bulk cleanup.

## Datasets and DataAPI

The bundled trainer implements **tracker-policy-v1** — snapshot → decision JSON. It is not a universal trainer for arbitrary architectures.

```json
{"input":{"as_of":"2026-01-01T00:00:00+00:00","slices":[{"slice_id":"1290:BR","clicks":300,"conversions":4,"profit_cents":70,"postbacks_mature":true}]},"output":[{"slice_id":"1290:BR","action":"scale","reason":"profit"}]}
```

- 1–3 slices per input; fields and `action`/`reason` values are validated by the trainer.
- All test timestamps must be strictly newer than every train timestamp; duplicate inputs and train/test intersections are rejected.
- Dataset versions are immutable — import a new version instead of overwriting; the SHA-256 of each import is recorded.
- Providers are a registry of **push** sources, not a background scheduler.

```bash
curl --fail-with-body https://dildo.asia/api/datasets/import \
  -H "Authorization: Bearer ***" \
  -H 'Content-Type: application/json' \
  --data-binary @dataset-payload.json
```

## Presets and release gates

| Preset | Steps | Gradient accumulation | Held-out examples |
| --- | ---: | ---: | ---: |
| smoke | 12 | 1 | up to 4 |
| standard | 160 | 4 | up to 16 |

LoRA rank 8, alpha 16, learning rate 2e-4, max length 640, seed 2026, deterministic decode (180 tokens).

Activation is verified **server-side**: completed job, adapter weights present, minimum held-out examples (8 by default), 100% valid JSON, ≥80% slice accuracy, no regression versus base. Smoke runs intentionally fail the example-count gate. The previously active adapter ID is retained.

`Download` returns a ZIP with the adapter, `job.json` and `report.json` — a **LoRA adapter**, not a merged full checkpoint. Activation switches the pointer used by `worker.py --predict` and the resident hot-testing server; it does not deploy a public inference endpoint.

## API

| Method | Path | Action |
| --- | --- | --- |
| POST | `/auth/register` · `/auth/login` · `/auth/logout` | Quick accounts; register/login are public and set the session cookie |
| GET | `/status` | Workers, models, datasets, jobs, settings, GPU |
| GET | `/jobs` | Run history (duration, GPU cost, metrics) |
| GET | `/progress/{job}` | Progress, loss series, log tail |
| GET | `/report/{job}` | Base/LoRA report + current release gates |
| GET | `/export/{job}` | Download the JSON report |
| GET | `/artifacts/{job}` | Download the adapter + metadata ZIP |
| GET | `/gpu/state` · `/operations/{id}` | GPU state / operation status & log |
| POST | `/predict` | Run snapshots through an adapter (up to 20) |
| POST | `/jobs/delete` | Delete a finished run and its adapter |
| POST | `/datasets/validate` · `/datasets/import` · `/datasets/demo` | Dataset management |
| POST | `/providers/save` | `{"name","description"}` |
| POST | `/jobs/queue` · `/jobs/cancel` · `/jobs/activate` | Run management |
| POST | `/settings` | `{"criteria":{…}}` / `{"gpu":{…}}` |
| POST | `/gpu/search,status,health,up` | `{}`; asynchronous operation |
| POST | `/gpu/down` | `{"instance_id":123}` — confirmation against the saved ID |
| POST | `/gpu/cycle` | `{"dataset","preset"}` — paid automatic run |

## Verification and limits

```bash
python3 -m unittest discover -s tests -v
node --check assets/ftstudio.js
python3 -m py_compile ft_node.py gpu_vast.py worker.py trainer.py predict_server.py
```

Tests use temporary directories and a mocked Vast API; they never rent GPUs. They cover validation/leakage/duplicates, immutable imports, release gates, missing artifacts, heartbeat, settings, cancellation, structured metrics, HTTP/assets/**register-login-sessions** and GPU cleanup/ownership. The UI is verified separately in a browser.

## Files

- `ft_node.py` — control plane, auth, API and UI serving.
- `predict_server.py` — resident hot-testing server (base model stays in RAM, adapters hot-swapped).
- `trainer.py` — LoRA training + evaluation + `metrics.jsonl`.
- `worker.py` — local queue and remote `--executor vast --once`.
- `gpu_vast.py` — provisioning / SSH / collect / cleanup, per-run cost recording.
- `assets/` — the portal (`index.html`, `ftstudio.js`, `portal.css`, bundled styles).
- `deploy/` — env and nginx examples. `tests/` — free-to-run checks.

## Sources

Instance search/create/delete paths were checked against the official [Vast.ai REST API](https://vast.ai/developers/api); the SSH instance format matches the [official CLI](https://github.com/vast-ai/vast-python). A contract check does not replace a smoke run on your own account and image.
