#!/usr/bin/env python3
"""Contract tests for the Vast cold-start scaffold. Stdlib unittest only."""
import json
import importlib.util
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'image' / 'z-image'))


def start_server(output_dir):
    os.environ['ZIMAGE_ENGINE'] = 'fake'
    os.environ['ZIMAGE_OUTPUT_DIR'] = str(output_dir)
    os.environ['ZIMAGE_MODEL_ID'] = 'Tongyi-MAI/Z-Image-Turbo'
    import server as srv
    server = srv.create_server(host='127.0.0.1', port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f'http://127.0.0.1:{server.server_address[1]}'


def request(url, method='GET', body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={'Content-Type': 'application/json', **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        e.close()
        try:
            return e.code, json.loads(body)
        except ValueError:
            return e.code, {'error': {'message': body}}


class VastApiTests(unittest.TestCase):
    def test_offers_accepts_gpu_names_as_list(self):
        spec = importlib.util.spec_from_file_location(
            'vast_deploy_under_test', ROOT / 'scripts' / 'vast_deploy.py')
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        class FakeVast(module.VastAPI):
            def __init__(self):
                self.last = None

            def request(self, method, path, body=None):
                self.last = (method, path, body)
                return []

        cfg = json.loads((ROOT / 'config.example.json').read_text())
        api = FakeVast()
        api.offers(cfg)
        query = urllib.parse.parse_qs(api.last[1].split('?', 1)[1])['q'][0]
        self.assertEqual(json.loads(query)['gpu_name'], {'in': cfg['gpu']['gpu_names']})


class CatalogTests(unittest.TestCase):
    def test_catalog_snapshot_has_all_models_and_grouped_markdown(self):
        raw = json.loads((ROOT / 'catalog' / 'hotapi-models.json').read_text())
        ids = [x['id'] for x in raw['data']]
        md = (ROOT / 'catalog' / 'models.md').read_text()
        self.assertEqual(len(ids), 67)
        self.assertIn('z-image-spicy', ids)
        self.assertIn('67 endpoint', md)
        self.assertIn('34 families', md)
        for mid in ids:
            self.assertIn(mid, md)


class ServerContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.server, self.base = start_server(self.tmp.name)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def wait_task(self, tid):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            code, task = request(f'{self.base}/v1/tasks/{tid}')
            if task['status'] in ('succeeded', 'failed', 'cancelled'):
                return code, task
            time.sleep(0.05)
        self.fail('task did not reach terminal state')

    def test_health_and_model_list_work_without_gpu(self):
        code, health = request(f'{self.base}/healthz')
        self.assertEqual(code, 200)
        self.assertEqual(health['status'], 'ok')
        self.assertEqual(health['engine'], 'fake')
        code, models = request(f'{self.base}/v1/models')
        self.assertEqual(code, 200)
        self.assertIn('z-image-spicy', [m['id'] for m in models['data']])

    def test_zimage_task_produces_downloadable_asset(self):
        code, task = request(f'{self.base}/v1/z-image-spicy', 'POST',
                             {'prompt': 'a fox', 'width': 768, 'height': 512, 'seed': 7})
        self.assertEqual(code, 202)
        self.assertEqual(task['model'], 'z-image-spicy')
        code, done = self.wait_task(task['id'])
        self.assertEqual(code, 200)
        self.assertEqual(done['status'], 'succeeded')
        asset = done['output']['assets'][0]
        self.assertEqual(asset['type'], 'image')
        with urllib.request.urlopen(asset['url'], timeout=10) as r:
            self.assertTrue(r.read().startswith(b'\x89PNG\r\n\x1a\n'))

    def test_unknown_model_and_bad_dimensions_are_rejected(self):
        code, err = request(f'{self.base}/v1/not-a-model', 'POST', {'prompt': 'x'})
        self.assertEqual(code, 404)
        self.assertIn('error', err)
        code, err = request(f'{self.base}/v1/z-image-spicy', 'POST',
                            {'prompt': 'x', 'width': 7, 'height': 512})
        self.assertEqual(code, 422)
        self.assertIn('width', json.dumps(err))

    def test_same_idempotency_key_replays_original_task(self):
        headers = {'Idempotency-Key': 'req-fixed-1'}
        body = {'prompt': 'same'}
        code, first = request(f'{self.base}/v1/z-image-spicy', 'POST', body, headers)
        code2, second = request(f'{self.base}/v1/z-image-spicy', 'POST', body, headers)
        self.assertEqual((code, code2), (202, 200))
        self.assertEqual(first['id'], second['id'])
        code3, _ = request(f'{self.base}/v1/z-image-spicy', 'POST',
                           {'prompt': 'different'}, headers)
        self.assertEqual(code3, 409)


class ImageArtifactTests(unittest.TestCase):
    def test_bootstrap_exports_runtime_env_and_starts_server(self):
        text = (ROOT / 'image' / 'z-image' / 'bootstrap.sh').read_text()
        self.assertIn('ZIMAGE_MODEL_ID', text)
        self.assertIn('ZIMAGE_PORT', text)
        self.assertIn('python3 server.py', text)
        self.assertIn('healthz', text)

    def test_bootstrap_installs_requirements_by_default(self):
        text = (ROOT / 'image' / 'z-image' / 'bootstrap.sh').read_text()
        self.assertIn('ZIMAGE_INSTALL_DEPS', text)
        self.assertIn('pip install', text)

    def test_dockerfile_installs_runtime_and_exposes_port(self):
        dockerfile = (ROOT / 'image' / 'z-image' / 'Dockerfile').read_text()
        requirements = (ROOT / 'image' / 'z-image' / 'requirements.txt').read_text()
        self.assertIn('diffusers', requirements)
        self.assertIn('torch', dockerfile)
        self.assertIn('EXPOSE 8096', dockerfile)


class DeployDryRunTests(unittest.TestCase):
    def run_deploy(self, state_dir, extra=None):
        cmd = [sys.executable, str(ROOT / 'scripts' / 'vast_deploy.py'), 'deploy',
               '--config', str(ROOT / 'config.example.json'),
               '--state-dir', str(state_dir), '--dry-run'] + (extra or [])
        return subprocess.run(cmd, capture_output=True, text=True, timeout=60)

    def test_dry_run_provisions_healthchecks_smokes_and_releases(self):
        state = Path(tempfile.mkdtemp()) / 'state'
        r = self.run_deploy(state)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        out = json.loads(r.stdout)
        self.assertTrue(out['smoke']['ok'])
        self.assertTrue(out['released'])
        active = json.loads((state / 'instance.json').read_text())
        self.assertFalse(active.get('id'))
        self.assertTrue(active['released_id'])

    def test_dry_run_refuses_second_instance_while_one_is_tracked(self):
        state = Path(tempfile.mkdtemp()) / 'state'
        state.mkdir(parents=True)
        (state / 'instance.json').write_text(json.dumps({'id': 12345, 'label': 'zimg-cs-existing'}))
        r = self.run_deploy(state)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('tracked instance', r.stdout + r.stderr)

    def test_dry_run_smoke_waits_for_terminal_task_and_checks_png(self):
        state = Path(tempfile.mkdtemp()) / 'state'
        r = self.run_deploy(state)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual(out['smoke']['status'], 'succeeded')
        self.assertTrue(out['smoke']['asset_checked'])


if __name__ == '__main__':
    unittest.main()
