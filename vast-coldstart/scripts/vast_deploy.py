#!/usr/bin/env python3
"""Controller-side cold-start deploy for one custom-model image on Vast.ai.

Adapted from the repo's gpu_vast.py safety rules:
  - exactly one tracked instance, identified by a unique label;
  - no automatic create retry after an ambiguous network answer;
  - secrets stay on the controller and are redacted from errors;
  - deletion is limited to the tracked instance.
`--dry-run` replaces both Vast API and SSH with in-process fakes: no key, no
instance, no money. It exercises the same state machine and health/smoke path.
"""
import argparse
import fcntl
import json
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATE = ROOT / 'state'


def utc():
    return datetime.now(timezone.utc).isoformat()


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {} if default is None else default


def write_json(path, obj):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + '\n')


def redact(message, secret):
    text = str(message)
    if secret:
        text = text.replace(secret, '[redacted]')
    return text


class VastAPI:
    def __init__(self, cfg, secret=None):
        self.base = cfg.get('vast_api', 'https://console.vast.ai/api/v0').rstrip('/')
        self.secret = secret or ''
        if not self.secret:
            raise RuntimeError('VAST_API_KEY is not configured')

    def request(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={'Authorization': 'Bearer ' + self.secret, 'Content-Type': 'application/json'},
        )
        result = None
        for attempt in range(6):
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    result = json.load(r) or {}
                break
            except urllib.error.HTTPError as e:
                if e.code != 429 or attempt == 5:
                    raise RuntimeError(f'Vast API HTTP {e.code}') from None
                time.sleep(min(30, 3 * (attempt + 1)))
            except urllib.error.URLError as e:
                if attempt == 5:
                    raise RuntimeError('Vast API unreachable: ' + str(e.reason)) from None
                time.sleep(min(30, 3 * (attempt + 1)))
        if isinstance(result, dict) and (result.get('success') is False or result.get('error')):
            raise RuntimeError('Vast API rejected request: ' + str(result.get('msg', result.get('error')))[:200])
        return result

    def offers(self, cfg):
        g = cfg['gpu']
        query = {
            'type': 'ondemand', 'rentable': {'eq': True}, 'rented': {'eq': False},
            'verified': {'eq': True}, 'num_gpus': {'eq': 1},
            'gpu_ram': {'gte': int(g['min_vram_gb'] * 1000)},
            'disk_space': {'gte': int(g['disk_gb'])},
            'dph_total': {'lte': float(g['max_dph'])},
            'reliability': {'gte': float(g.get('reliability', 0.95))},
            'limit': 30,
        }
        raw_names = g.get('gpu_names', '')
        if isinstance(raw_names, str):
            names = [x.strip() for x in raw_names.split(',') if x.strip()]
        else:
            names = [str(x).strip() for x in (raw_names or []) if str(x).strip()]
        if names:
            query['gpu_name'] = {'in': names}
        result = self.request('GET', '/bundles/?' + urllib.parse.urlencode({'q': json.dumps(query)}))
        rows = result if isinstance(result, list) else result.get('offers', [])
        rows = [{k: o.get(k) for k in ('id', 'ask_contract_id', 'gpu_name', 'gpu_ram', 'dph_total',
                                      'reliability2', 'disk_space', 'geolocation')} for o in rows]
        rows = [o for o in rows if float(o.get('dph_total') or 999) <= float(g['max_dph'])]
        rows.sort(key=lambda o: float(o['dph_total']))
        return rows

    def instances(self):
        result = self.request('GET', '/instances/')
        return result if isinstance(result, list) else result.get('instances', [])

    def create(self, offer, cfg):
        ic = cfg['instance']
        label = ic.get('label_prefix', 'zimg-cs-') + uuid.uuid4().hex[:8]
        body = {'disk': cfg['gpu']['disk_gb'], 'label': label, 'runtype': ic.get('runtype', 'ssh')}
        if ic.get('template_hash'):
            body['template_hash_id'] = ic['template_hash']
        else:
            body['image'] = ic['image']
        # One create call only: an ambiguous answer must never create a second bill.
        result = self.request('PUT', f"/asks/{offer.get('ask_contract_id') or offer['id']}/", body)
        iid = result.get('new_contract') or result.get('instance_id')
        if not iid:
            raise RuntimeError('Vast response has no instance ID; inspect the console before retrying')
        return iid, label

    def destroy(self, iid):
        return self.request('DELETE', f'/instances/{iid}/')


class MockAPI:
    """Deterministic in-process Vast double for --dry-run."""

    def __init__(self):
        self.created = []
        self.deleted = []
        self.all_instances = []

    def offers(self, cfg):
        return [{
            'id': 101, 'ask_contract_id': 101, 'gpu_name': 'RTX 3090 (mock)',
            'gpu_ram': 24576, 'dph_total': 0.35, 'reliability2': '0.99',
            'disk_space': 80, 'geolocation': 'mock',
        }]

    def instances(self):
        return list(self.all_instances)

    def create(self, offer, cfg):
        iid = 900000 + len(self.created) + 1
        label = cfg['instance'].get('label_prefix', 'zimg-cs-') + uuid.uuid4().hex[:8]
        self.created.append({'id': iid, 'label': label})
        self.all_instances.append({
            'id': iid, 'label': label, 'actual_status': 'running',
            'ssh_host': '127.0.0.1', 'ssh_port': 22,
            'gpu_name': offer['gpu_name'], 'dph_total': offer['dph_total'],
        })
        return iid, label

    def destroy(self, iid):
        self.deleted.append(iid)
        self.all_instances = [x for x in self.all_instances if str(x['id']) != str(iid)]
        return {'deleted': True}


class SSHTransport:
    def __init__(self, key_path, known_hosts):
        self.key = str(key_path)
        self.known = str(known_hosts)

    def args(self, inst):
        host = str(inst.get('ssh_host', ''))
        port = str(inst.get('ssh_port', ''))
        if not re.fullmatch(r'[a-zA-Z0-9.-]+', host) or not port.isdigit():
            raise RuntimeError('Instance has no valid SSH endpoint yet')
        if not Path(self.key).is_file():
            raise RuntimeError('SSH key is missing: ' + self.key)
        return (['-i', self.key, '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=12',
                 '-o', 'StrictHostKeyChecking=accept-new', '-o', 'UserKnownHostsFile=' + self.known,
                 '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3'], host, port)

    def ssh(self, inst, command, timeout=45):
        opts, host, port = self.args(inst)
        r = subprocess.run(['ssh', *opts, '-p', port, 'root@' + host, command],
                           text=True, capture_output=True, timeout=timeout)
        if r.returncode:
            raise RuntimeError('SSH failed: ' + r.stderr[-500:])
        return r.stdout

    def scp(self, inst, source, dest, timeout=600):
        opts, host, port = self.args(inst)
        r = subprocess.run(['scp', *opts, '-P', port, '-r', str(source), 'root@' + host + ':' + dest],
                           text=True, capture_output=True, timeout=timeout)
        if r.returncode:
            raise RuntimeError('scp failed: ' + r.stderr[-500:])


class MockTransport:
    """Deterministic in-process Vast double for --dry-run."""

    def ssh(self, inst, command, timeout=45):
        if 'nvidia-smi' in command:
            return 'NVIDIA RTX 3090 (mock), 24576 MB\n'
        if '/healthz' in command:
            return json.dumps({'status': 'ok', 'engine': 'zimage', 'model': 'z-image-spicy'})
        if '-X POST' in command:
            return json.dumps({
                'id': 'task_mock0000001', 'status': 'queued', 'model': 'z-image-spicy',
            })
        if '/v1/tasks/task_mock0000001' in command:
            return json.dumps({
                'id': 'task_mock0000001', 'status': 'succeeded', 'model': 'z-image-spicy',
                'output': {'assets': [{
                    'type': 'image',
                    'path': '/workspace/zimage-output/task_mock0000001/0.png',
                }]},
            })
        if '/files/task_mock0000001/0.png' in command:
            return 'PNG_OK'
        return ''

    def scp(self, inst, source, dest, timeout=600):
        return None


def load_secret(cfg):
    return os.getenv('VAST_API_KEY') or (Path(cfg['vast_key_file']).read_text().strip()
                                         if cfg.get('vast_key_file') and Path(cfg['vast_key_file']).is_file() else '')


def tracked(api, state):
    saved = read_json(state / 'instance.json')
    if not saved.get('id'):
        return None
    for inst in api.instances():
        if str(inst.get('id')) == str(saved['id']):
            if inst.get('label') != saved.get('label'):
                raise RuntimeError('Instance ownership label mismatch')
            return inst
    return None


def wait_ready(api, transport, state, cfg, deadline):
    while True:
        if time.monotonic() > deadline:
            raise TimeoutError('Instance did not become ready in time')
        inst = tracked(api, state)
        if inst:
            saved = read_json(state / 'instance.json')
            saved.update({k: inst.get(k) for k in ('actual_status', 'gpu_name', 'dph_total',
                                                   'ssh_host', 'ssh_port')})
            saved['updated_at'] = utc()
            write_json(state / 'instance.json', saved)
            if inst.get('actual_status') == 'running' and inst.get('ssh_host') and inst.get('ssh_port'):
                try:
                    info = transport.ssh(inst, 'nvidia-smi --query-gpu=name,memory.total --format=csv,noheader', 20)
                    return inst, info.strip()
                except (RuntimeError, subprocess.TimeoutExpired):
                    pass
        time.sleep(2 if not cfg.get('_dry_run') else 0)


def wait_health(transport, inst, cfg, deadline):
    port = cfg['remote']['port']
    cmd = f'curl -fsS http://127.0.0.1:{port}/healthz'
    while time.monotonic() < deadline:
        try:
            raw = transport.ssh(inst, cmd, 20)
            payload = json.loads(raw.strip().splitlines()[-1])
            if payload.get('status') == 'ok':
                return payload
        except (RuntimeError, ValueError, subprocess.TimeoutExpired):
            pass
        time.sleep(2 if not cfg.get('_dry_run') else 0)
    raise TimeoutError('Server did not pass healthcheck in time')


def _ssh_json(transport, inst, cmd, timeout=30):
    raw = transport.ssh(inst, cmd, timeout)
    return json.loads(raw.strip().splitlines()[-1])


def smoke(transport, inst, cfg):
    port = cfg['remote']['port']
    model = cfg['model']['public_id']
    base = f'http://127.0.0.1:{port}'
    payload = json.dumps(cfg['smoke'], ensure_ascii=False)
    submit = (
        f"curl -fsS -X POST {base}/v1/{model} "
        f"-H 'Content-Type: application/json' -d {shlex.quote(payload)}"
    )
    task = _ssh_json(transport, inst, submit, int(cfg['checks']['smoke_timeout_s']))
    tid = task.get('id') or task.get('task_id')
    if not tid:
        return {'ok': False, 'error': 'no task id in submit response'}

    deadline = time.monotonic() + int(cfg['checks']['smoke_timeout_s'])
    last = task
    while time.monotonic() < deadline:
        last = _ssh_json(transport, inst, f'curl -fsS {base}/v1/tasks/{tid}', 20)
        if last.get('status') in ('succeeded', 'failed', 'cancelled'):
            break
        time.sleep(0 if cfg.get('_dry_run') else 2)

    if last.get('status') != 'succeeded':
        return {'ok': False, 'task_id': tid, 'status': last.get('status', 'timeout')}

    png_cmd = (
        f"curl -fsS {base}/files/{tid}/0.png -o /tmp/zimg-smoke.png "
        "&& python3 -c 'import pathlib,sys; b=pathlib.Path(\"/tmp/zimg-smoke.png\").read_bytes(); "
        "sys.exit(0 if b.startswith(bytes([137,80,78,71,13,10,26,10])) else 1)'"
    )
    try:
        transport.ssh(inst, png_cmd, 30)
        asset_checked = True
    except RuntimeError:
        asset_checked = False
    return {
        'ok': asset_checked,
        'task_id': tid,
        'status': 'succeeded',
        'asset_checked': asset_checked,
    }


def deploy(cfg, state, api, transport):
    saved = read_json(state / 'instance.json')
    if saved.get('id'):
        raise RuntimeError('A tracked instance exists; inspect and release it first')
    offers = api.offers(cfg)
    blacklist = {str(x) for x in read_json(state / 'blacklist.json').get('contracts', [])}
    usable = [o for o in offers if str(o.get('ask_contract_id') or o.get('id')) not in blacklist]
    if not usable and offers:
        usable = offers  # do not refuse every offer forever
    write_json(state / 'offers.json', {'offers': usable, 'updated_at': utc()})
    if not usable:
        raise RuntimeError('No offers match GPU, price and reliability limits')
    offer = usable[0]
    write_json(state / 'last-offer.json', {'contract': offer.get('ask_contract_id') or offer.get('id'),
                                           'gpu': offer.get('gpu_name'), 'at': utc()})
    iid, label = api.create(offer, cfg)  # no retry
    write_json(state / 'instance.json', {
        'id': iid, 'label': label, 'actual_status': 'provisioning', 'created_at': utc(),
        'gpu_name': offer.get('gpu_name'), 'dph_total': offer.get('dph_total'),
    })
    created = True
    result = {}
    try:
        now = time.monotonic()
        inst, gpu_info = wait_ready(api, transport, state, cfg,
                                    now + int(cfg['checks']['ready_timeout_s']))
        remote = cfg['remote']['dir']
        out = cfg['remote']['output_dir']
        transport.ssh(inst, f'mkdir -p {shlex.quote(out)} {shlex.quote(remote)}')
        transport.scp(inst, ROOT / 'image' / 'z-image', remote + '/app')
        m = cfg['model']
        env = {
            'ZIMAGE_MODEL_ID': m['hf_model'], 'ZIMAGE_PUBLIC_MODEL': m['public_id'],
            'ZIMAGE_PRECISION': m.get('precision', 'bf16'), 'ZIMAGE_STEPS': str(m.get('steps', 9)),
            'ZIMAGE_PORT': str(cfg['remote']['port']), 'ZIMAGE_OUTPUT_DIR': out,
            'ZIMAGE_ENGINE': 'fake' if cfg.get('_dry_run') else 'auto',
        }
        if m.get('lora_dir'):
            env['ZIMAGE_LORA_DIR'] = m['lora_dir']
        env_prefix = ' '.join(f'{k}={shlex.quote(str(v))}' for k, v in env.items())
        transport.ssh(inst, f'cd {shlex.quote(remote + "/app")} && nohup env {env_prefix} '
                            f'python3 server.py > {shlex.quote(remote + "/server.log")} 2>&1 < /dev/null &')
        health = wait_health(transport, inst, cfg,
                             time.monotonic() + int(cfg['checks']['health_timeout_s']))
        result['smoke'] = smoke(transport, inst, cfg)
        if not result['smoke']['ok']:
            raise RuntimeError('Smoke generation did not return a task')
        result.update({'instance_id': iid, 'label': label, 'gpu': gpu_info,
                       'dph_total': offer.get('dph_total'), 'health': health})
        if cfg.get('release_after_smoke', True):
            api.destroy(iid)
            created = False
            write_json(state / 'instance.json', {
                'released_id': iid, 'released_at': utc(), 'id': None,
                'reason': 'released_after_smoke',
            })
            result['released'] = True
        else:
            result['released'] = False
        return result
    except Exception:
        try:
            if created:
                inst = tracked(api, state)
                if inst:
                    logs = transport.ssh(inst, f'tail -c 12000 {shlex.quote(cfg["remote"]["dir"] + "/server.log")}')
                    write_json(state / 'last-logs.json', {'captured_at': utc(), 'log_tail': logs})
        except Exception:
            pass
        raise
    finally:
        if created:
            try:
                api.destroy(iid)
                write_json(state / 'instance.json', {'released_id': iid, 'released_at': utc(),
                                                     'id': None, 'reason': 'cleanup_after_error'})
            except Exception:
                # Keep the ID so the operator can release it manually.
                pass


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=['search', 'deploy', 'status', 'release'])
    p.add_argument('--config', default=str(ROOT / 'config.example.json'))
    p.add_argument('--state-dir', default=str(DEFAULT_STATE))
    p.add_argument('--dry-run', action='store_true')
    args = p.parse_args()
    cfg = json.loads(Path(args.config).read_text())
    state = Path(args.state_dir)
    state.mkdir(parents=True, exist_ok=True)
    cfg['_dry_run'] = args.dry_run
    secret = '' if args.dry_run else load_secret(cfg)
    api = MockAPI() if args.dry_run else VastAPI(cfg, secret)
    if args.dry_run:
        transport = MockTransport()
    else:
        transport = SSHTransport(cfg['ssh_key'], state / 'known_hosts')

    with (state / 'deploy.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            if args.action == 'search':
                out = {'offers': api.offers(cfg)}
            elif args.action == 'deploy':
                out = deploy(cfg, state, api, transport)
            elif args.action == 'status':
                inst = tracked(api, state)
                out = {'instance': read_json(state / 'instance.json'), 'found': bool(inst)}
            else:
                inst = tracked(api, state)
                iid = (read_json(state / 'instance.json').get('id') if inst else None)
                if iid:
                    api.destroy(iid)
                    write_json(state / 'instance.json', {'released_id': iid,
                                                         'released_at': utc(), 'id': None})
                out = {'released': bool(iid), 'id': iid}
            print(json.dumps(out, indent=2, ensure_ascii=False), flush=True)
            return 0
        except Exception as exc:
            print(json.dumps({'error': redact(exc, secret)}, ensure_ascii=False), flush=True)
            return 1


if __name__ == '__main__':
    sys.exit(main())
