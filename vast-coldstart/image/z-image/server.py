#!/usr/bin/env python3
"""Cold-start image API for the first custom-model image (Z-Image).

No torch/diffusers import at module import time: /healthz and task bookkeeping
work on a bare Python host, and the real engine is loaded lazily on first
generation. Set ZIMAGE_ENGINE=fake to exercise the full API contract on a CPU.
"""
import base64
import io
import json
import os
import re
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PUBLIC_MODEL = os.getenv('ZIMAGE_PUBLIC_MODEL', 'z-image-spicy')
HF_MODEL = os.getenv('ZIMAGE_MODEL_ID', 'Tongyi-MAI/Z-Image-Turbo')
LORA_DIR = os.getenv('ZIMAGE_LORA_DIR', '')
PRECISION = os.getenv('ZIMAGE_PRECISION', 'bf16')
STEPS = int(os.getenv('ZIMAGE_STEPS', '9'))
MIN_SIDE = 256
MAX_SIDE = 1536
TASK_ID = re.compile(r'^task_[0-9a-f]{12}$')
MAX_BODY = 1024 * 1024

TASKS = {}
IDEMPOTENCY = {}
LOCK = threading.Lock()
ENGINE = None

# A valid 1x1 PNG: lets the fake engine exercise the whole download path.
_FAKE_PNG = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg=='
)


class FakeEngine:
    name = 'fake'

    def generate(self, prompt, width, height, seed):
        return _FAKE_PNG


class ZImageEngine:
    name = 'zimage'

    def __init__(self):
        import torch
        from diffusers import ZImagePipeline
        self.torch = torch
        dtype = torch.bfloat16 if PRECISION == 'bf16' else torch.float16
        self.pipe = ZImagePipeline.from_pretrained(
            HF_MODEL, torch_dtype=dtype, low_cpu_mem_usage=True,
        )
        if LORA_DIR:
            self.pipe.load_lora_weights(LORA_DIR)
        if torch.cuda.is_available():
            self.pipe.to('cuda')
        else:
            self.pipe.enable_model_cpu_offload()
        self.device = 'cuda' if torch.cuda.is_available() else 'cpu'

    def generate(self, prompt, width, height, seed):
        torch = self.torch
        generator = None
        if seed is not None:
            device = 'cuda' if torch.cuda.is_available() else 'cpu'
            generator = torch.Generator(device=device).manual_seed(int(seed))
        image = self.pipe(
            prompt=prompt,
            height=height,
            width=width,
            num_inference_steps=STEPS,
            guidance_scale=0.0,
            generator=generator,
        ).images[0]
        buf = io.BytesIO()
        image.save(buf, format='PNG')
        return buf.getvalue()


def get_engine():
    global ENGINE
    with LOCK:
        if ENGINE is None:
            mode = os.getenv('ZIMAGE_ENGINE', 'auto')
            ENGINE = FakeEngine() if mode == 'fake' else ZImageEngine()
        return ENGINE


def output_dir():
    return Path(os.getenv('ZIMAGE_OUTPUT_DIR', '/var/lib/vast-coldstart/output'))


def validate(body, model):
    if not isinstance(body, dict):
        return 'body must be an object'
    prompt = body.get('prompt')
    if not isinstance(prompt, str) or not prompt.strip():
        return 'prompt is required'
    for key in ('width', 'height'):
        value = body.get(key, 1024)
        if isinstance(value, bool) or not isinstance(value, int):
            return f'{key} must be an integer'
        if value < MIN_SIDE or value > MAX_SIDE or value % 8:
            return f'{key} must be a multiple of 8 between {MIN_SIDE} and {MAX_SIDE}'
    seed = body.get('seed')
    if seed is not None and (isinstance(seed, bool) or not isinstance(seed, int)):
        return 'seed must be an integer'
    return None


def create_task(model, body, idem_key=None):
    with LOCK:
        canonical = json.dumps(body, sort_keys=True)
        if idem_key:
            replay = IDEMPOTENCY.get(idem_key)
            if replay:
                if replay['body'] != canonical:
                    return None, 'conflict'
                return TASKS[replay['tid']], True
        tid = 'task_' + uuid.uuid4().hex[:12]
        task = {
            'id': tid,
            'status': 'queued',
            'model': model,
            'estimated_credits_cost': 26,
            'webhook_url': None,
            'created_at': int(time.time()),
        }
        TASKS[tid] = task
        if idem_key:
            IDEMPOTENCY[idem_key] = {'body': canonical, 'tid': tid}
    threading.Thread(target=generate, args=(tid, dict(body)), daemon=True).start()
    return task, False


def generate(tid, params):
    task = TASKS[tid]
    try:
        with LOCK:
            task['status'] = 'processing'
        engine = get_engine()
        payload = engine.generate(
            params.get('prompt', ''),
            int(params.get('width', 1024)),
            int(params.get('height', 1024)),
            params.get('seed'),
        )
        target = output_dir() / tid / '0.png'
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        with LOCK:
            task['output'] = {'assets': [{'type': 'image', 'path': str(target)}]}
            task['status'] = 'succeeded'
            task['actual_credits_cost'] = 26
            task['completed_at'] = int(time.time())
    except Exception as exc:
        with LOCK:
            task['status'] = 'failed'
            task['error'] = {'message': str(exc)[:300]}


def public_task(task, handler):
    result = dict(task)
    assets = result.get('output', {}).get('assets')
    if assets:
        host = handler.headers.get('Host', '127.0.0.1')
        assets = [dict(a, url=f'http://{host}/files/{task["id"]}/0.png') for a in assets]
        result['output'] = {'assets': assets}
    return result


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, format, *args):
        pass

    def send_json(self, code, payload):
        raw = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def read_body(self):
        length = int(self.headers.get('Content-Length') or 0)
        if length > MAX_BODY:
            raise ValueError('body too large')
        raw = self.rfile.read(length) if length else b''
        return json.loads(raw or b'{}')

    def do_GET(self):
        path = urllib_parse_path(self.path)
        if path == '/healthz':
            engine = os.getenv('ZIMAGE_ENGINE', 'auto')
            return self.send_json(200, {
                'status': 'ok', 'engine': engine, 'model': PUBLIC_MODEL,
                'hf_model': HF_MODEL, 'output_dir': str(output_dir()),
            })
        if path == '/v1/models':
            return self.send_json(200, {'object': 'list', 'data': [{
                'id': PUBLIC_MODEL, 'object': 'model', 'owned_by': 'self-hosted',
                'modal': 'image', 'task_kind': 'image.generate',
                'hf_model': HF_MODEL,
            }]})
        m = re.fullmatch(r'/v1/tasks/([^/]+)', path)
        if m:
            tid = m.group(1)
            if not TASK_ID.fullmatch(tid) or tid not in TASKS:
                return self.send_json(404, {'error': {'message': 'task not found'}})
            with LOCK:
                return self.send_json(200, public_task(TASKS[tid], self))
        m = re.fullmatch(r'/files/(task_[0-9a-f]{12})/0\.png', path)
        if m:
            target = output_dir() / m.group(1) / '0.png'
            if not target.is_file():
                return self.send_json(404, {'error': {'message': 'asset not found'}})
            raw = target.read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', 'image/png')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        return self.send_json(404, {'error': {'message': 'not found'}})

    def do_POST(self):
        path = urllib_parse_path(self.path)
        supported = {'/v1/' + PUBLIC_MODEL, '/v1/' + PUBLIC_MODEL + '/text-to-image', '/v1/tasks'}
        if path not in supported or self.command != 'POST':
            return self.send_json(404, {'error': {'message': 'model not found'}})
        try:
            body = self.read_body()
        except (ValueError, json.JSONDecodeError):
            return self.send_json(422, {'error': {'message': 'invalid JSON body'}})
        model = PUBLIC_MODEL
        if path == '/v1/tasks':
            model = body.get('model')
            if model != PUBLIC_MODEL:
                return self.send_json(404, {'error': {'message': 'model not found'}})
            body = body.get('input') or {}
        error = validate(body, model)
        if error:
            return self.send_json(422, {'error': {'message': error, 'field': error.split()[0]}})
        key = self.headers.get('Idempotency-Key')
        task, replay = create_task(model, body, key)
        if replay == 'conflict':
            return self.send_json(409, {'error': {'message': 'Idempotency-Key reused with a different body'}})
        self.send_json(200 if replay else 202, public_task(task, self))


def urllib_parse_path(raw):
    return raw.split('?', 1)[0]


def create_server(host='127.0.0.1', port=None):
    if port is None:
        port = int(os.getenv('ZIMAGE_PORT', '8096'))
    return ThreadingHTTPServer((host, port), Handler)


def main():
    server = create_server()
    print(f'z-image coldstart API on {server.server_address[1]} · model={PUBLIC_MODEL} '
          f'· hf={HF_MODEL} · engine={os.getenv("ZIMAGE_ENGINE", "auto")}', flush=True)
    server.serve_forever()


if __name__ == '__main__':
    main()
