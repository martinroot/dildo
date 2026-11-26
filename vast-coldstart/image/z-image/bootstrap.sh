#!/usr/bin/env bash
# Cold-start entrypoint inside the Vast instance.
# Controller polls /healthz before running the smoke generation.
set -euo pipefail

export ZIMAGE_PORT="${ZIMAGE_PORT:-8096}"
export ZIMAGE_OUTPUT_DIR="${ZIMAGE_OUTPUT_DIR:-/var/lib/vast-coldstart/output}"
export ZIMAGE_MODEL_ID="${ZIMAGE_MODEL_ID:-Tongyi-MAI/Z-Image-Turbo}"
export ZIMAGE_PUBLIC_MODEL="${ZIMAGE_PUBLIC_MODEL:-z-image-spicy}"
export ZIMAGE_PRECISION="${ZIMAGE_PRECISION:-bf16}"
export ZIMAGE_STEPS="${ZIMAGE_STEPS:-9}"
export ZIMAGE_ENGINE="${ZIMAGE_ENGINE:-auto}"
export ZIMAGE_PRELOAD="${ZIMAGE_PRELOAD:-1}"

mkdir -p "$ZIMAGE_OUTPUT_DIR"

if command -v nvidia-smi >/dev/null 2>&1; then
  echo "[bootstrap] GPU: $(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader | head -1)"
else
  echo "[bootstrap] WARNING: nvidia-smi not found (fake-engine smoke mode is fine)"
fi

echo "[bootstrap] model=$ZIMAGE_MODEL_ID engine=$ZIMAGE_ENGINE port=$ZIMAGE_PORT"
if [ "$ZIMAGE_PRELOAD" = "1" ] && [ "$ZIMAGE_ENGINE" != "fake" ]; then
  echo "[bootstrap] preloading weights; first start downloads from HuggingFace"
  python3 -c 'import server; server.get_engine()'
fi

exec python3 server.py
