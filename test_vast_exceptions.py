"""Offline tests for the exception-handling paths of the patched Vast executor."""
import importlib.util, json, shutil, sys, tempfile
from pathlib import Path
sys.path.insert(0, '/opt/ft-node')

spec = importlib.util.spec_from_file_location('gpu_vast_patched', '/home/dildo/.hermes/cache/scratch/gpu_vast_patched.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

JID = '20261112184521-d28b7d7bd313'
BASE = Path(tempfile.mkdtemp(prefix='ft-exc-test-'))
JDIR = BASE / 'jobs' / JID
JDIR.mkdir(parents=True)
m.job_dir = lambda jid: JDIR
m.FT_DIR = BASE
cache = BASE / '.cache/huggingface/hub/models--Qwen--Qwen3.5-0.8B-Base'
cache.mkdir(parents=True, exist_ok=True)
(cache / 'weights.bin').write_text('dummy')
m.write_json(JDIR / 'job.json', {'id': JID, 'executor': 'vast', 'dataset': 'data/' + JID})


class FakeVast(m.Vast):
    def __init__(self, scenario):
        self.scenario = scenario
        self.destroyed = 0
        self.cfg = {'max_minutes': 50}
        self.deadline = 1e12
        self.job = None
        self.commands = []

    def tracked(self): return {'id': 123, 'ssh_host': 'host.example', 'ssh_port': 22, 'label': 'ft-node-test'}
    def store(self, inst): pass
    def create(self): pass
    def ready(self): return self.tracked()
    def check(self): pass
    def copy(self, *a, **k): pass
    def collect_dir(self, inst, source, dest, *a, **k):
        result = Path(dest) / 'result'
        (result / 'adapter').mkdir(parents=True, exist_ok=True)
        (result / 'report.json').write_text('{}')
        (result / 'adapter' / 'adapter_model.safetensors').write_text('x')
    def upload_chunked(self, *a, **k):
        if self.scenario == 'upload_timeout':
            raise m.CycleFailure('chunk 2/4 SCP timed out', 'transient', 'upload')
    def remote_json(self, inst, path):
        if self.scenario == 'stale_meta' and path.endswith('job.json'):
            return {'id': 'OTHER-JOB'}
        if path.endswith('status.json'):
            if self.scenario == 'remote_fail': return {'state': 'failed', 'message': 'CUDA OOM at step 10', 'updated_at': '2026-11-26T00:00:00+00:00'}
            if self.scenario == 'heartbeat_dead': return {'state': 'running', 'phase': 'training'}
            return {'state': 'completed', 'phase': 'done'}
        if path.endswith('worker.json'):
            return {'updated_at': '2000-01-01T00:00:00+00:00'} if self.scenario == 'heartbeat_dead' else {'updated_at': '2099-01-01T00:00:00+00:00'}
        return {}
    def ssh(self, inst, command, timeout=45):
        self.commands.append(command)
        if 'nohup bash' in command and self.scenario == 'ssh_transient':
            raise m.CycleFailure('SSH timeout after 45s', 'transient', 'ssh')
        if 'tail -c 6000' in command: return ''
        return ''
    def destroy(self):
        self.destroyed += 1
        return {'released': True}


def reset():
    for name in ('status.json', 'errors.jsonl', 'progress.json', 'gpu.json', 'setup.log'):
        (JDIR / name).unlink(missing_ok=True)
    shutil.rmtree(JDIR / 'result', ignore_errors=True)


def run(scenario):
    reset()
    v = FakeVast(scenario)
    try:
        v.cycle(JID)
        outcome = 'success'
    except Exception as exc:
        outcome = getattr(exc, 'category', 'fatal')
    status = json.loads((JDIR / 'status.json').read_text())
    errors = (JDIR / 'errors.jsonl').read_text() if (JDIR / 'errors.jsonl').exists() else ''
    print('%-16s outcome=%-10s state=%-10s category=%-10s destroyed=%d errors=%d' % (
        scenario, outcome, status.get('state'), status.get('error_category'), v.destroyed,
        len([x for x in errors.splitlines() if x])))
    return outcome, status, v.destroyed, errors


results = {}
for scenario in ('remote_fail', 'heartbeat_dead', 'ssh_transient', 'upload_timeout', 'stale_meta'):
    results[scenario] = run(scenario)

assert results['remote_fail'][1]['error_category'] == 'remote'
assert results['remote_fail'][2] == 1, 'remote failure must release after salvage'
assert results['heartbeat_dead'][1]['error_category'] == 'remote'
assert results['ssh_transient'][1]['error_category'] == 'transient'
assert results['ssh_transient'][2] == 0, 'transient error must keep the instance'
assert 'cuda' not in results['ssh_transient'][1]['message'].lower()
assert results['upload_timeout'][1]['state'] == 'completed', 'cache timeout must not fail the job'
assert results['upload_timeout'][2] == 1, 'successful job releases instance'
assert 'warning' in results['upload_timeout'][3]
assert 'warning' in results['stale_meta'][3]

# malformed/truncated remote JSON recovery
v = FakeVast('success')
orig = v.ssh
v.ssh = lambda inst, command, timeout=45: '{"state":"completed",' + 'x' * 20
assert v.remote_json(None, '/x') == {}, 'malformed JSON must not raise'
print('ALL ASSERTIONS PASSED')
shutil.rmtree(BASE, ignore_errors=True)
