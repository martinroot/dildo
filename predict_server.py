#!/usr/bin/env python3
"""Resident prediction server: keeps the base model in RAM so portal "Тест"
returns in seconds instead of reloading weights per request.

Listens on 127.0.0.1:8095. POST JSON:
  {"input": <snapshot or [snapshots]>, "job": "<id>" (optional, else latest adapter), "max_new_tokens": 180}
Replies: {"job": "...", "count": N, "results": [...]}
Adapters are hot-swapped: the base model is loaded once, only the small
LoRA weights (~30 MB) are re-read when a different job is requested.
"""
import io
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

FT_DIR = Path(os.environ.get('FT_DIR', '/var/lib/ft-node'))
sys.path.insert(0, '/opt/ft-node')
os.chdir('/opt/ft-node')

import trainer  # noqa: E402

STATE = {'base': None, 'tokenizer': None, 'adapters': {}, 'default_job': None}
MAX_CACHE = 2


def default_job():
    active = FT_DIR / 'active.json'
    try:
        job = json.loads(active.read_text()).get('active')
        if job and (FT_DIR / 'jobs' / job / 'result/adapter/adapter_model.safetensors').is_file():
            return job
    except (OSError, ValueError):
        pass
    newest = ''
    jobs = list_jobs()
    if jobs:
        newest = jobs[0]
    return newest


def list_jobs():
    import json as _json
    out = []
    for p in sorted((FT_DIR / 'jobs').glob('*'), reverse=True):
        st = p / 'status.json'
        try:
            state = _json.loads(st.read_text()).get('state')
        except (OSError, ValueError):
            continue
        if state == 'completed' and (p / 'result/adapter/adapter_model.safetensors').is_file():
            out.append(p.name)
    return out


def resolve(job):
    if job and (FT_DIR / 'jobs' / job / 'result/adapter/adapter_model.safetensors').is_file():
        return job
    return default_job()


def load_adapter(job):
    from peft import PeftModel
    adapter = FT_DIR / 'jobs' / job / 'result/adapter'
    if STATE['base'] is None:
        STATE['base'], STATE['tokenizer'], _ = trainer.load_model(trainer.BASE)
        STATE['base'].eval()
    if job in STATE['adapters']:
        return STATE['adapters'][job]
    model = PeftModel.from_pretrained(STATE['base'], adapter)
    model.eval()
    STATE['adapters'][job] = model
    while len(STATE['adapters']) > MAX_CACHE:
        STATE['adapters'].pop(next(iter(STATE['adapters'])))
    return model


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _reply(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._reply(200, {'service': 'ft-predict', 'loaded': STATE['base'] is not None,
                          'default_job': default_job(), 'cached_adapters': list(STATE['adapters'])})

    def do_POST(self):
        try:
            length = int(self.headers.get('Content-Length', '0'))
            p = json.loads(self.rfile.read(length))
            snaps = p.get('input')
            if isinstance(snaps, dict):
                snaps = [snaps]
            if not isinstance(snaps, list) or not snaps or len(snaps) > 20:
                raise ValueError('input: one snapshot or up to 20 snapshots')
            job = resolve(str(p.get('job') or ''))
            if not job:
                raise ValueError('No trained adapter available')
            model = load_adapter(job)
            results = []
            for snap in snaps:
                trainer.validate_snapshot(snap)
                results.append(trainer.generate_actions(model, STATE['tokenizer'], snap, int(p.get('max_new_tokens') or 180)))
            self._reply(200, {'job': job, 'count': len(results), 'results': results})
        except (ValueError, KeyError) as exc:
            self._reply(422, {'error': str(exc)})
        except Exception as exc:  # keep the server alive on unexpected errors
            self._reply(500, {'error': type(exc).__name__ + ': ' + str(exc)[:300]})


if __name__ == '__main__':
    port = int(os.environ.get('FT_PREDICT_PORT', '8095'))
    print('ft-predict listening on 127.0.0.1:' + str(port), flush=True)
    ThreadingHTTPServer(('127.0.0.1', port), Handler).serve_forever()
