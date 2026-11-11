#!/usr/bin/env python3
"""DILDO / FineTune control plane. Standard library HTTP API; private by default."""
import base64
import hashlib
import hmac
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import threading
import uuid
from datetime import datetime as _dt, timezone
# Historical name kept so all existing call sites keep working.
datetime = _dt
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from trainer import validate_snapshot, validate_actions, timestamp

ROOT = Path(__file__).resolve().parent
FT_DIR = Path(os.getenv('FT_DIR', '/var/lib/ft-node')).resolve()
PORT = int(os.getenv('FT_PORT', '8094'))
JOB_ID = re.compile(r'[0-9]{14}-[a-f0-9]{12}')
NAME = re.compile(r'[A-Za-z0-9_-]{1,40}')
PRESETS = {'smoke': (12, 1, 4), 'standard': (160, 4, 16)}
LOCK = threading.RLock()
DEFAULT_CRITERIA = {'min_examples': 8, 'min_accuracy': 0.8, 'min_validity': 1.0, 'no_regression': True}
DEFAULT_GPU = {'max_dph': 0.8, 'min_vram_gb': 16, 'disk_gb': 40, 'max_minutes': 120,
               'gpu_names': 'RTX 4090,RTX 3090,A5000', 'template_hash': '',
               'image': 'pytorch/pytorch:2.6.0-cuda12.4-cudnn9-runtime'}

def utc(): return datetime.now(timezone.utc).isoformat()
def read_json(path):
    try: return json.loads(Path(path).read_text())
    except (OSError, ValueError): return {}
def write_json(path, value):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + '\n')
    tmp.replace(path)
def init():
    for sub in ('datasets','jobs','operations'): (FT_DIR / sub).mkdir(parents=True, exist_ok=True)
def valid_name(name):
    if not isinstance(name, str) or not NAME.fullmatch(name): raise ValueError('Use 1–40 letters, numbers, - or _.')
    return name
def job_dir(job):
    if not isinstance(job, str) or not JOB_ID.fullmatch(job): raise ValueError('Invalid job ID')
    d = FT_DIR / 'jobs' / job
    if not d.is_dir(): raise ValueError('Job not found')
    return d
def settings():
    saved = read_json(FT_DIR / 'settings.json')
    return {'criteria': {**DEFAULT_CRITERIA, **saved.get('criteria', {})}, 'gpu': {**DEFAULT_GPU, **saved.get('gpu', {})}}
def save_settings(p):
    cfg = settings()
    for group in ('criteria', 'gpu'):
        if group in p:
            if not isinstance(p[group], dict) or set(p[group]) - set(cfg[group]): raise ValueError('Unknown settings')
            cfg[group].update(p[group])
    c,g = cfg['criteria'],cfg['gpu']
    if type(c['min_examples']) is not int or not 1 <= c['min_examples'] <= 10000: raise ValueError('min_examples must be 1–10000')
    for key in ('min_accuracy','min_validity'):
        if type(c[key]) not in (int,float) or not 0 <= c[key] <= 1: raise ValueError(key + ' must be 0–1')
    if type(c['no_regression']) is not bool: raise ValueError('no_regression must be boolean')
    for key,lo,hi in [('max_dph',.01,100),('min_vram_gb',1,192),('disk_gb',10,1000),('max_minutes',5,720)]:
        if type(g[key]) not in (int,float) or not lo <= g[key] <= hi: raise ValueError(key + ' outside allowed range')
    for key in ('gpu_names','template_hash','image'):
        if not isinstance(g[key],str) or len(g[key]) > 200: raise ValueError('Invalid ' + key)
    write_json(FT_DIR/'settings.json',cfg)
    return cfg

def validate_text(train, test):
    stats = {'train':0,'test':0,'errors':[],'actions':{},'duplicates':0,'overlap':0,'schema':'tracker-policy-v1'}
    sets,times = {},{}
    for split,body in [('train',train),('test',test)]:
        if not isinstance(body,str): raise ValueError('train and test must be JSONL strings')
        seen=set(); dates=[]
        for i,line in enumerate(body.splitlines(),1):
            if not line.strip(): continue
            try:
                row=json.loads(line)
                if not isinstance(row,dict) or set(row) != {'input','output'}: raise ValueError('Expected input and output')
                validate_actions(row['output'],validate_snapshot(row['input']))
                key=json.dumps(row['input'],sort_keys=True)
                if key in seen: stats['duplicates']+=1
                seen.add(key); dates.append(timestamp(row['input']['as_of']))
                stats[split]+=1
                for item in row['output']:
                    label=item['action']+'/'+item['reason']; stats['actions'][label]=stats['actions'].get(label,0)+1
            except (ValueError,TypeError,KeyError) as exc:
                stats['errors'].append(f'{split}:{i}: {exc}')
        if not stats[split]: stats['errors'].append(f'{split}: no valid examples')
        sets[split]=seen; times[split]=dates
    stats['overlap']=len(sets['train'] & sets['test'])
    if stats['overlap']: stats['errors'].append('Train / test input leakage')
    if stats['duplicates']: stats['errors'].append('Duplicate inputs within a split')
    if times['train'] and times['test'] and max(times['train']) >= min(times['test']):
        stats['errors'].append('Test timestamps must be strictly later than all training timestamps')
    stats['ok']=not stats['errors']; stats['checked_at']=utc()
    return stats

def validate_dataset(p):
    if 'train' in p: return validate_text(p.get('train'),p.get('test'))
    name=valid_name(p.get('name')); d=FT_DIR/'datasets'/name
    if not (d/'train.jsonl').is_file() or not (d/'test.jsonl').is_file(): raise ValueError('Dataset is missing train.jsonl or test.jsonl')
    result=validate_text((d/'train.jsonl').read_text(),(d/'test.jsonl').read_text())
    write_json(d/'validation.json',result)
    return result

def import_dataset(p):
    name=valid_name(p.get('name')); train,test=p.get('train'),p.get('test')
    result=validate_text(train,test)
    if not result['ok']: raise ValueError('; '.join(result['errors'][:8]))
    with LOCK:
        d=FT_DIR/'datasets'/name
        if d.exists(): raise ValueError('Dataset versions are immutable. Choose a new name.')
        staging=FT_DIR/'datasets'/('.import-'+uuid.uuid4().hex); staging.mkdir()
        (staging/'train.jsonl').write_text(train); (staging/'test.jsonl').write_text(test)
        meta={'name':name,'train_rows':result['train'],'test_rows':result['test'], 'imported_at':utc(),
              'provider':str(p.get('provider','JSONL upload'))[:80], 'sha256':hashlib.sha256((train+'\n'+test).encode()).hexdigest()}
        write_json(staging/'meta.json',meta); write_json(staging/'validation.json',result); staging.rename(d)
    return {'imported':True,**meta}

def demo_dataset(p):
    from trainer import demo
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        d=Path(tmp); demo(d)
        return import_dataset({'name':p.get('name','demo-'+datetime.now().strftime('%H%M%S')), 'train':(d/'train.jsonl').read_text(), 'test':(d/'test.jsonl').read_text(), 'provider':'Synthetic demo · not production evidence'})

def dataset_list():
    return [{'name':d.name, **read_json(d/'meta.json'), 'validation':read_json(d/'validation.json')}
            for d in sorted((FT_DIR/'datasets').iterdir()) if d.is_dir() and NAME.fullmatch(d.name) and not JOB_ID.fullmatch(d.name)]

def gates(report):
    c=settings()['criteria']; b=report.get('base',{}); l=report.get('lora',{})
    def ratio(m,k,total): return m.get(k,0)/m.get(total,1) if m.get(total,0)>0 else 0
    acc=ratio(l,'exact_slices','total_slices'); base=ratio(b,'exact_slices','total_slices')
    checks=[{'name':'Held-out examples','actual':l.get('examples',0),'target':c['min_examples'],'pass':l.get('examples',0)>=c['min_examples']},
            {'name':'Valid JSON','actual':ratio(l,'valid_json_and_schema','examples'),'target':c['min_validity'],'pass':bool(l.get('examples')) and ratio(l,'valid_json_and_schema','examples')>=c['min_validity']},
            {'name':'Slice accuracy','actual':acc,'target':c['min_accuracy'],'pass':bool(l.get('total_slices')) and acc>=c['min_accuracy']},
            {'name':'No regression','actual':acc-base,'target':0,'pass':not c['no_regression'] or bool(b.get('total_slices')) and acc>=base}]
    return {'passed':all(x['pass'] for x in checks),'checks':checks}

def queue_job(p):
    preset=p.get('preset','smoke'); device=p.get('device','cpu'); executor=p.get('executor','local')
    if preset not in PRESETS or device not in ('cpu','cuda') or executor not in ('local','vast'): raise ValueError('Invalid preset, device or executor')
    name=valid_name(p.get('dataset')); validation=validate_dataset({'name':name})
    if not validation['ok']: raise ValueError('; '.join(validation['errors'][:8]))
    jid=datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')+'-'+os.urandom(6).hex(); d=FT_DIR/'jobs'/jid; d.mkdir()
    # Snapshot bytes, not symlinks: a run must remain reproducible after provider updates.
    snapshot=FT_DIR/'datasets'/jid; shutil.copytree(FT_DIR/'datasets'/name,snapshot)
    model=os.getenv('FT_REMOTE_MODEL' if executor=='vast' else 'RTB_FINETUNE_MODEL','Qwen/Qwen3.5-0.8B-Base')
    meta={'id':jid,'preset':preset,'dataset':jid,'source_dataset':name,'device':'cuda' if executor=='vast' else device,
          'executor':executor,'model':model,'created_at':utc(),'criteria':settings()['criteria'],
          'dataset_sha256':read_json(snapshot/'meta.json').get('sha256')}
    write_json(d/'job.json',meta); write_json(d/'status.json',{'state':'queued','phase':'queued','updated_at':utc()})
    return {'queued':True,**meta}

def activate(p):
    d=job_dir(p.get('job'))
    if read_json(d/'status.json').get('state')!='completed': raise ValueError('Only completed runs can be activated')
    report=read_json(d/'result/report.json'); verdict=gates(report)
    if not verdict['passed']: raise ValueError('Release blocked: '+', '.join(c['name'] for c in verdict['checks'] if not c['pass']))
    if not (d/'result/adapter/adapter_model.safetensors').is_file(): raise ValueError('Adapter artifact is missing')
    with LOCK:
        prev=read_json(FT_DIR/'active.json'); reg={'active':d.name,'previous':prev.get('active'),'updated_at':utc(),'criteria':settings()['criteria']}
        write_json(FT_DIR/'active.json',reg)
    return reg

def cancel(p):
    d=job_dir(p.get('job')); state=read_json(d/'status.json').get('state')
    if state not in ('queued','running'): raise ValueError('Job is already terminal')
    write_json(d/'cancel.json',{'at':utc()})
    if state=='queued': write_json(d/'status.json',{'state':'cancelled','phase':'cancelled','updated_at':utc()})
    return {'cancel_requested':True}

def job_list():
    rows=[]
    for d in sorted((FT_DIR/'jobs').iterdir(),reverse=True):
        if not JOB_ID.fullmatch(d.name): continue
        meta=read_json(d/'job.json'); report=read_json(d/'result/report.json'); gpu=read_json(d/'gpu.json')
        # Wall-clock duration: job.json creation → last status update.
        duration=None
        try:
            started=_dt.fromisoformat(meta['created_at'])
            finished=_dt.fromisoformat(read_json(d/'status.json').get('updated_at') or gpu.get('ended_at') or '')
            if finished and finished>=started: duration=round((finished-started).total_seconds())
        except (KeyError,ValueError,TypeError): pass
        rows.append({**meta,'status':read_json(d/'status.json'), 'progress':read_json(d/'progress.json'),
                     'metrics':{k:report[k] for k in ('base','lora') if k in report}, 'gates':gates(report) if report else None,
                     'artifact_available':(d/'result/adapter/adapter_model.safetensors').is_file(),
                     'gpu_cost':gpu,'duration_s':duration})
    return rows

def progress_view(jid):
    d=job_dir(jid); log=d/'worker.log'; series=[]
    history=d/'metrics.jsonl'
    if history.is_file():
        for line in history.read_text().splitlines():
            try: series.append(json.loads(line))
            except ValueError: pass
    tail=''
    if log.is_file():
        with log.open('rb') as f:
            f.seek(max(0,log.stat().st_size-16000)); tail=f.read().decode(errors='replace')
    return {'progress':read_json(d/'progress.json'),'log_tail':tail,'loss_series':series}

def gpu_key_present():
    return bool(os.getenv('VAST_API_KEY')) or Path(os.getenv('FT_VAST_KEY','/etc/ft-node/vast_api_key')).is_file() or (FT_DIR/'vast_api_key').is_file()

def save_vast_key(p):
    """Store a vast.ai API key from the portal into FT_DIR (chmod 600)."""
    key=str(p.get('vast_api_key') or '').strip()
    if not re.fullmatch(r'[0-9a-zA-Z_-]{20,120}',key): raise ValueError('That does not look like a vast.ai API key')
    (FT_DIR/'vast_api_key').write_text(key+'\n')
    (FT_DIR/'vast_api_key').chmod(0o600)
    return {'saved':True}
def gpu_state():
    ops=[read_json(p) for p in sorted((FT_DIR/'operations').glob('*.json'),reverse=True)][:12]
    return {'configured':gpu_key_present(),'ssh_configured':Path(os.getenv('FT_SSH_KEY','/etc/ft-node/id_ed25519')).is_file(),
            'instance':read_json(FT_DIR/'gpu-instance.json'),'operations':ops,'offers':read_json(FT_DIR/'gpu-offers.json').get('offers',[])}
def gpu_run(action,p):
    if action not in ('search','status','health','up','down','cycle'): raise ValueError('Unknown GPU action')
    if not gpu_key_present(): raise ValueError('Set VAST_API_KEY or FT_VAST_KEY on the server first')
    if action in ('up','cycle') and not Path(os.getenv('FT_SSH_KEY','/etc/ft-node/id_ed25519')).is_file(): raise ValueError('Configure FT_SSH_KEY; add its public key to your Vast account')
    if action=='down' and str(p.get('instance_id'))!=str(read_json(FT_DIR/'gpu-instance.json').get('id')): raise ValueError('Instance confirmation does not match')
    with LOCK:
        for op in (FT_DIR/'operations').glob('*.json'):
            old=read_json(op)
            if old.get('state')=='running':
                try:
                    pid=old.get('pid')
                    if not isinstance(pid,int) or pid<1: raise OSError('No process ID')
                    os.kill(pid,0)
                except (OSError,TypeError):
                    old.update(state='failed',error='Operation process stopped. Inspect the instance before restarting.'); write_json(op,old)
                else: raise ValueError('A GPU operation is already running')
        jid=None
        if action=='cycle': jid=queue_job({**p,'executor':'vast','device':'cuda'})['id']
        oid=datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')+'-'+uuid.uuid4().hex[:8]
        meta={'id':oid,'action':action,'state':'running','created_at':utc(),'job':jid}
        write_json(FT_DIR/'operations'/(oid+'.json'),meta)
        with (FT_DIR/'operations'/(oid+'.log')).open('w') as log:
            proc=subprocess.Popen([sys.executable,str(ROOT/'gpu_vast.py'),action,'--operation',oid]+(['--job',jid] if jid else []),stdout=log,stderr=subprocess.STDOUT,env={**os.environ,'FT_DIR':str(FT_DIR)},start_new_session=True)
        meta={**meta, **read_json(FT_DIR/'operations'/(oid+'.json')), 'pid':proc.pid}; write_json(FT_DIR/'operations'/(oid+'.json'),meta)
    return {'started':True,**meta}

def status_view():
    hb=read_json(FT_DIR/'worker.json'); age=None
    try: age=(datetime.now(timezone.utc)-datetime.fromisoformat(hb['updated_at'])).total_seconds()
    except (KeyError,ValueError,TypeError): pass
    return {'service':'dildo-finetune','now':utc(),'worker':{**hb,'online':age is not None and 0<=age<30,'age_seconds':age},
            'registry':read_json(FT_DIR/'active.json'),'datasets':[x['name'] for x in dataset_list()], 'dataset_details':dataset_list(),
            'jobs':job_list(),'settings':settings(),'gpu':gpu_state(),'providers':read_json(FT_DIR/'providers.json').get('items',[]),
            'base_model':os.getenv('RTB_FINETUNE_MODEL','Qwen/Qwen3.5-0.8B-Base')}

def provider_save(p):
    name=valid_name(p.get('name')); label=str(p.get('description',''))[:200]
    with LOCK:
        rows=read_json(FT_DIR/'providers.json').get('items',[])
        rows=[x for x in rows if x['name']!=name]+[{'name':name,'description':label,'type':'push-jsonl','updated_at':utc()}]
        write_json(FT_DIR/'providers.json',{'items':rows})
    return {'items':rows}

def _secret():
    p = FT_DIR / 'session_secret'
    if not p.is_file():
        p.write_text(os.urandom(32).hex())
        p.chmod(0o600)
    return p.read_text().strip()

def _pw_hash(pw, salt):
    return hashlib.pbkdf2_hmac('sha256', pw.encode(), bytes.fromhex(salt), 200_000).hex()

def users_file(): return FT_DIR / 'users.json'

def user_register(p):
    login = str(p.get('login') or '').strip().lower()
    pw = str(p.get('password') or '')
    if not re.fullmatch(r'[a-z0-9_.-]{3,32}', login): raise ValueError('Login: 3–32 chars, a-z 0-9 _ . -')
    if len(pw) < 6: raise ValueError('Password must be at least 6 characters')
    rows = read_json(users_file()).get('users', [])
    if any(u['login'] == login for u in rows): raise ValueError('This login is already taken')
    salt = os.urandom(16).hex()
    rows.append({'login': login, 'salt': salt, 'hash': _pw_hash(pw, salt), 'created_at': utc()})
    write_json(users_file(), {'users': rows})
    return user_login(p)

def user_login(p):
    login = str(p.get('login') or '').strip().lower()
    pw = str(p.get('password') or '')
    rows = read_json(users_file()).get('users', [])
    user = next((u for u in rows if u['login'] == login), None)
    if not user or not hmac.compare_digest(user['hash'], _pw_hash(pw, user['salt'])):
        raise ValueError('Wrong login or password')
    msg = login + ':' + utc()
    sig = hmac.new(bytes.fromhex(_secret()), msg.encode(), hashlib.sha256).hexdigest()
    token = base64.urlsafe_b64encode(msg.encode()).decode().rstrip('=') + '.' + sig
    return {'ok': True, 'login': login, 'token': token, 'max_age': 60 * 60 * 24 * 30}

def session_login(header):
    """Return the login for a valid 'login:stamp.sig' token, else None."""
    try:
        payload, sig = header.rsplit('.', 1)
        msg = base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)).decode()
        good = hmac.new(bytes.fromhex(_secret()), msg.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, good): return None
        login, stamp = msg.split(':', 1)
        # Tokens older than 30 days expire.
        if (datetime.now(timezone.utc) - datetime.fromisoformat(stamp)).total_seconds() > 60 * 60 * 24 * 30: return None
        return login
    except (ValueError, TypeError, IndexError): return None

def user_logout():
    return {'ok': True}

def job_delete(p):
    """Delete a finished run together with its adapter artifact. The active model is protected."""
    jid = str(p.get('job') or '')
    if not re.fullmatch(r'[0-9]{14}-[a-f0-9]{12,16}', jid): raise ValueError('Invalid job id')
    d = job_dir(jid)
    if not d.is_dir(): raise ValueError('Unknown job')
    if read_json(FT_DIR / 'active.json').get('active') == jid:
        raise ValueError('This adapter is active. Activate another model before deleting it.')
    shutil.rmtree(d)
    return {'deleted': jid}


def predict_hot(p, snaps):
    """Ask the resident model server (fast path). Returns None when it is not running."""
    import urllib.request
    body = json.dumps({**p, 'input': snaps}).encode()
    req = urllib.request.Request('http://127.0.0.1:8095/predict', data=body, method='POST',
                                 headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return json.load(r)
    except (OSError, ValueError):
        return None


def predict_test(p):
    """Run the trained adapter on user-supplied snapshots and return its decisions."""
    snaps = p.get('input')
    if isinstance(snaps, dict): snaps = [snaps]
    if not isinstance(snaps, list) or not snaps or len(snaps) > 20:
        raise ValueError('input: one snapshot object or up to 20 snapshots')
    hot = predict_hot(p, snaps)
    if hot is not None: return hot
    job = str(p.get('job') or '')
    if not job:
        job = str(read_json(FT_DIR / 'active.json').get('active') or '')
    if not job:
        # Fall back to the newest completed run that returned an adapter.
        for meta in job_list():
            if meta.get('status', {}).get('state') == 'completed' and (job_dir(meta['id']) / 'result/adapter/adapter_model.safetensors').is_file():
                job = meta['id']; break
    if not job: raise ValueError('No trained adapter yet — complete a training run first')
    adapter = job_dir(job) / 'result/adapter'
    if not (adapter / 'adapter_model.safetensors').is_file():
        raise ValueError('Adapter artifact missing for ' + job)
    inp = FT_DIR / 'predict-input.jsonl'
    inp.write_text('\n'.join(json.dumps(s, ensure_ascii=False) for s in snaps) + '\n')
    try:
        r = subprocess.run([sys.executable, str(ROOT / 'trainer.py'), 'predict',
                            '--adapter', str(adapter), '--input', str(inp),
                            '--max-new-tokens', str(int(p.get('max_new_tokens') or 180))],
                           capture_output=True, text=True, timeout=900)
    except subprocess.TimeoutExpired:
        raise ValueError('Prediction timed out (model too large for this host?)')
    if r.returncode:
        raise ValueError('Predict failed: ' + (r.stderr or '')[-300:])
    results = []
    for line in r.stdout.splitlines():
        line = line.strip()
        if line.startswith(('{', '[')):
            try: results.append(json.loads(line))
            except ValueError: pass
    return {'job': job, 'count': len(results), 'results': results}


class Handler(BaseHTTPRequestHandler):
    def reply(self,code,payload,ctype='application/json; charset=utf-8',attachment=None,cookie=None):
        body=json.dumps(payload,ensure_ascii=False,allow_nan=False).encode() if isinstance(payload,(dict,list)) else payload
        self.send_response(code); self.send_header('Content-Type',ctype); self.send_header('Content-Length',str(len(body)))
        self.send_header('Cache-Control','no-store'); self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")
        if attachment: self.send_header('Content-Disposition','attachment; filename="'+attachment+'"')
        if cookie: self.send_header('Set-Cookie',cookie)
        self.end_headers(); self.wfile.write(body)
    def authed(self):
        """Valid bearer token, authenticated proxy header, or a valid session cookie."""
        token=os.getenv('FT_API_TOKEN')
        if token and hmac.compare_digest(self.headers.get('Authorization',''),'Bearer '+token): return True
        if token and self.headers.get('Authorization','').startswith('Bearer '): return False
        if self.headers.get('X-FT-Authenticated')=='1': return True
        cookie=self.headers.get('Cookie','')
        for part in cookie.split(';'):
            k,_,v=part.strip().partition('=')
            if k=='ft_session' and session_login(v): return True
        return False
    def session_cookie(self,payload):
        maxage=int(payload.get('max_age') or 60*60*24*30)
        return f"ft_session={payload['token']}; Max-Age={maxage}; Path=/; HttpOnly; SameSite=Lax"
    def guard(self):
        allowed={'127.0.0.1','localhost',*filter(None,os.getenv('FT_ALLOWED_HOSTS','').split(','))}
        if (self.headers.get('Host') or '').split(':')[0] not in allowed:
            self.reply(403,{'error':'Host not allowed; configure reverse proxy Host 127.0.0.1'}); return False
        origin=self.headers.get('Origin')
        if self.command=='POST' and origin and urlsplit(origin).netloc != (self.headers.get('X-Forwarded-Host') or self.headers.get('Host')):
            self.reply(403,{'error':'Cross-origin request rejected'}); return False
        return True
    def artifact_download(self, jid):
        import tempfile
        import zipfile
        d=job_dir(jid)
        if read_json(d/'status.json').get('state')!='completed' or not (d/'result/adapter/adapter_model.safetensors').is_file():
            raise ValueError('Completed adapter artifact is unavailable')
        with tempfile.TemporaryFile() as tmp:
            with zipfile.ZipFile(tmp,'w',compression=zipfile.ZIP_STORED) as archive:
                for f in (d/'result/adapter').rglob('*'):
                    if f.is_file() and not f.is_symlink(): archive.write(f,'adapter/'+str(f.relative_to(d/'result/adapter')))
                for f in (d/'job.json',d/'result/report.json'):
                    if f.is_file(): archive.write(f,f.name)
            size=tmp.tell(); tmp.seek(0)
            self.send_response(200); self.send_header('Content-Type','application/zip')
            self.send_header('Content-Length',str(size)); self.send_header('Cache-Control','no-store')
            self.send_header('Content-Disposition','attachment; filename="'+jid+'-adapter.zip"')
            self.send_header('X-Content-Type-Options','nosniff'); self.end_headers()
            shutil.copyfileobj(tmp,self.wfile,length=1024*1024)
    def do_GET(self):
        if not self.guard(): return
        path=urlsplit(self.path).path
        try:
            # The static shell is public: it renders the login screen when the session is missing.
            if path in ('/','/ui','/ui/'):
                return self.reply(200,(ROOT/'assets/index.html').read_bytes(),'text/html; charset=utf-8')
            if path.startswith('/assets/'):
                name=path.removeprefix('/assets/')
                if '/' in name or name.startswith('.'): return self.reply(404,{'error':'Not found'})
                f=ROOT/'assets'/name
                if f.is_file(): return self.reply(200,f.read_bytes(),mimetypes.guess_type(str(f))[0] or 'application/octet-stream')
            if not self.authed(): return self.reply(401,{'error':'Authentication required','auth':True})
            if path=='/status': return self.reply(200,status_view())
            if path=='/jobs': return self.reply(200,{'jobs':job_list()})
            if path=='/gpu/state': return self.reply(200,gpu_state())
            if path.startswith('/progress/'): return self.reply(200,progress_view(path.split('/')[-1]))
            if path.startswith('/report/'):
                report=read_json(job_dir(path.split('/')[-1])/'result/report.json')
                if not report: return self.reply(404,{'error':'No report yet'})
                return self.reply(200,{**report,'gates':gates(report)})
            if path.startswith('/operations/'):
                oid=path.split('/')[-1]
                if not re.fullmatch(r'[0-9]{14}-[a-f0-9]{8}',oid): raise ValueError('Invalid operation')
                p=FT_DIR/'operations'/(oid+'.log'); text=p.read_text(errors='replace')[-20000:] if p.is_file() else ''
                return self.reply(200,{**read_json(p.with_suffix('.json')),'log':text})
            if path.startswith('/artifacts/'):
                if not self.authed(): return self.reply(401,{'error':'Authentication required','auth':True})
                return self.artifact_download(path.split('/')[-1])
            if path.startswith('/export/'):
                d=job_dir(path.split('/')[-1]); report=read_json(d/'result/report.json')
                if not report: raise ValueError('No report yet')
                return self.reply(200,json.dumps(report,indent=2).encode(),'application/json',d.name+'-report.json')
            self.reply(404,{'error':'Route not found'})
        except (ValueError,OSError) as exc: self.reply(422,{'error':str(exc)})
    def do_POST(self):
        if not self.guard(): return
        public_auth={'/auth/register','/auth/login'}
        if urlsplit(self.path).path not in public_auth and not self.authed(): return self.reply(401,{'error':'Authentication required','auth':True})
        try:
            if self.headers.get_content_type()!='application/json': return self.reply(415,{'error':'application/json required'})
            length=int(self.headers.get('Content-Length','0'))
            if not 0<length<=16*1024*1024: return self.reply(413,{'error':'Request must be 1 byte–16 MiB'})
            p=json.loads(self.rfile.read(length))
            if not isinstance(p,dict): raise ValueError('Expected JSON object')
            routes={'/datasets/import':import_dataset,'/datasets/validate':validate_dataset,'/datasets/demo':demo_dataset,
                    '/gpu/demo-dataset':demo_dataset,'/jobs/queue':queue_job,'/jobs/cancel':cancel,'/jobs/activate':activate,
                    '/settings':save_settings,'/providers/save':provider_save,'/predict':predict_test,'/jobs/delete':job_delete,
                    '/auth/register':user_register,'/auth/login':user_login,'/auth/logout':user_logout,'/vast-key':save_vast_key}
            public={'/auth/register','/auth/login'}
            if self.path not in public and not self.authed(): return self.reply(401,{'error':'Authentication required','auth':True})

            if self.path.startswith('/gpu/') and self.path not in routes: result=gpu_run(self.path.split('/')[-1],p)
            elif self.path in routes:
                if self.path=='/jobs/queue' and p.get('executor')=='vast': raise ValueError('Use POST /gpu/cycle for remote execution')
                result=routes[self.path](p)
            else: return self.reply(404,{'error':'Route not found'})
            cookie=None
            if self.path in ('/auth/login','/auth/register') and result.get('token'): cookie=self.session_cookie(result)
            if self.path=='/auth/logout': cookie='ft_session=; Max-Age=0; Path=/; HttpOnly; SameSite=Lax'
            self.reply(200,result,cookie=cookie)
        except (ValueError,TypeError,KeyError) as exc: self.reply(422,{'error':str(exc)})
        except OSError as exc: self.reply(500,{'error':str(exc)})
    def log_message(self,fmt,*args): sys.stderr.write('%s %s\n'%(self.log_date_time_string(),fmt%args))

def main():
    os.umask(0o077); init()
    ThreadingHTTPServer((os.getenv('FT_BIND','127.0.0.1'),PORT),Handler).serve_forever()
if __name__=='__main__': main()
