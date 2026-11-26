#!/usr/bin/env python3
"""Vast.ai FineTune executor: provision → SSH → train → collect → destroy.
Secrets stay on the controller. Only a tracked instance with our exact label is mutable.
"""
import argparse
import traceback
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time
import urllib.request
import urllib.parse
import urllib.error
from ft_node import FT_DIR, ROOT, read_json, write_json, settings, utc, job_dir, init
import uuid
import shutil

API='https://console.vast.ai/api/v0'
KEY_PATH=Path(os.getenv('FT_VAST_KEY','/etc/ft-node/vast_api_key'))
# The portal can also save the key into FT_DIR; check it as a fallback.
PORTAL_KEY=FT_DIR/'vast_api_key'
SSH_KEY=Path(os.getenv('FT_SSH_KEY','/etc/ft-node/id_ed25519'))
SSH_KNOWN=FT_DIR/'known_hosts'


class CycleFailure(RuntimeError):
    """Failure with a recovery category.

    fatal     - configuration/data error: no retry until fixed
    transient - network/ssh/timeout: keep the instance, the next cycle resumes it
    remote    - remote job failed: salvage logs/artifacts before releasing
    cancelled - operator cancelled: salvage before releasing
    """
    CATEGORIES = ('fatal', 'transient', 'remote', 'cancelled')

    def __init__(self, message, category='fatal', phase=None):
        super().__init__(str(message))
        self.category = category if category in self.CATEGORIES else 'fatal'
        self.phase = phase


def secrets():
    values = []
    for name in ('VAST_API_KEY', 'HF_TOKEN', 'HUGGING_FACE_HUB_TOKEN'):
        value = os.getenv(name) or ''
        if len(value) > 6:
            values.append(value)
    try:
        if KEY_PATH.is_file():
            value = KEY_PATH.read_text().strip()
            if len(value) > 6:
                values.append(value)
    except OSError:
        pass
    return values


def redact(text):
    result = str(text)
    for value in secrets():
        result = result.replace(value, '[REDACTED]')
    return result


def failure_parts(error):
    if isinstance(error, InterruptedError):
        return 'cancelled', 'Операция отменена'
    if isinstance(error, CycleFailure):
        return error.category, redact(error)
    return 'fatal', redact(error)


def log_event(directory, category, phase, message, error=None):
    entry = {'at': utc(), 'category': category, 'phase': phase, 'message': redact(message)}
    if error is not None:
        entry.update({'type': type(error).__name__, 'traceback': redact(traceback.format_exc())[-4000:]})
    try:
        with Path(directory).joinpath('errors.jsonl').open('a', encoding='utf-8') as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + '\n')
    except OSError:
        pass


def record_failure(directory, error, phase=None):
    """Single place for every finetune failure: state, category, message, traceback."""
    category, message = failure_parts(error)
    write_json(directory / 'status.json', {
        'state': 'cancelled' if category == 'cancelled' else 'failed',
        'phase': phase or getattr(error, 'phase', None) or 'error',
        'error_category': category, 'error_type': type(error).__name__,
        'message': message, 'updated_at': utc(),
    })
    log_event(directory, category, phase, message, error)
    print('ERROR [%s/%s]: %s' % (category, phase or 'error', message), flush=True)
class Vast:
    def __init__(self):
        self.key=os.getenv('VAST_API_KEY') or (KEY_PATH.read_text().strip() if KEY_PATH.is_file() else '') or (PORTAL_KEY.read_text().strip() if PORTAL_KEY.is_file() else '')
        if not self.key: raise ValueError('VAST_API_KEY is not configured')
        self.cfg=settings()['gpu']; self.deadline=time.monotonic()+self.cfg['max_minutes']*60
        self.job=None
    def api(self,method,path,body=None):
        data=json.dumps(body).encode() if body is not None else None
        req=urllib.request.Request(API+path,data=data,method=method,headers={'Authorization':'Bearer '+self.key,'Content-Type':'application/json'})
        # The API rate-limits bursts (HTTP 429): back off and retry instead of failing the turn.
        for attempt in range(6):
            try:
                with urllib.request.urlopen(req,timeout=30) as r: result=json.load(r)
                break
            except urllib.error.HTTPError as exc:
                if exc.code!=429 or attempt==5:
                    category='transient' if exc.code in (429,500,502,503,504) else 'fatal'
                    raise CycleFailure(f'Vast API returned HTTP {exc.code}',category,'provider') from None
                time.sleep(min(60, 5*(attempt+1)))
            except urllib.error.URLError as exc:
                if attempt==5: raise CycleFailure('Vast API unreachable: '+redact(exc.reason),'transient','provider') from None
                time.sleep(min(60, 5*(attempt+1)))
        if isinstance(result,dict) and (result.get('success') is False or result.get('error')):
            raise CycleFailure('Vast API rejected request: '+redact(result.get('msg',result.get('error')))[:200],'fatal','provider')
        return result
    def check(self):
        if time.monotonic()>self.deadline: raise TimeoutError('Configured GPU time limit reached')
        if self.job and (self.job/'cancel.json').exists(): raise InterruptedError('Cancellation requested')
    def offers(self):
        c=self.cfg
        q={'type':'ondemand','rentable':{'eq':True},'rented':{'eq':False},'verified':{'eq':True},
           'num_gpus':{'eq':1},'gpu_ram':{'gte':c['min_vram_gb']*1000},'disk_space':{'gte':c['disk_gb']},
           'dph_total':{'lte':c['max_dph']},'reliability':{'gte':.95},'limit':30}
        names=[x.strip() for x in c['gpu_names'].split(',') if x.strip()]
        if names: q['gpu_name']={'in':names}
        result=self.api('GET','/bundles/?'+urllib.parse.urlencode({'q':json.dumps(q)}))
        offers=result if isinstance(result,list) else result.get('offers',[])
        rows=[{k:o.get(k) for k in ('id','ask_contract_id','gpu_name','gpu_ram','dph_total','reliability2','disk_space','geolocation')} for o in offers]
        rows=[o for o in rows if float(o.get('dph_total') or 999)<=c['max_dph']]
        rows.sort(key=lambda o:float(o['dph_total']))
        # Hosts that repeatedly failed before opening SSH are excluded from future rentals.
        bad={str(x) for x in read_json(FT_DIR/'gpu-blacklist.json').get('contracts',[])}
        usable=[o for o in rows if str(o.get('ask_contract_id') or o.get('id')) not in bad]
        if usable or not rows: rows=usable
        else: write_json(FT_DIR/'gpu-blacklist.json',{'contracts':bad,'note':'all current offers blacklisted'})
        write_json(FT_DIR/'gpu-offers.json',{'offers':rows,'updated_at':utc()})
        return rows
    def instances(self):
        result=self.api('GET','/instances/'); return result if isinstance(result,list) else result.get('instances',[])
    def tracked(self):
        state=read_json(FT_DIR/'gpu-instance.json')
        if not state.get('id'): return None
        for inst in self.instances():
            if str(inst.get('id'))==str(state['id']):
                if inst.get('label')!=state.get('label'): raise RuntimeError('Instance ownership label mismatch')
                return inst
        return None
    def store(self,inst):
        state=read_json(FT_DIR/'gpu-instance.json')
        state.update({k:inst.get(k) for k in ('id','label','actual_status','gpu_name','dph_total','ssh_host','ssh_port')})
        state['updated_at']=utc(); write_json(FT_DIR/'gpu-instance.json',state)
    def create(self):
        if read_json(FT_DIR/'gpu-instance.json').get('id'): raise ValueError('A tracked instance exists. Inspect and release it first.')
        offers=self.offers()
        if not offers: raise ValueError('No offers match price, VRAM and reliability limits')
        offer=offers[0]; write_json(FT_DIR/'gpu-last-offer.json',{'contract':offer.get('ask_contract_id') or offer.get('id'),'gpu':offer.get('gpu_name'),'at':utc()}); c=self.cfg; label='ft-node-'+os.urandom(6).hex()
        body={'disk':c['disk_gb'],'label':label,'runtype':'ssh','image':c['image']}
        if c['template_hash']: body={'disk':c['disk_gb'],'label':label,'template_hash_id':c['template_hash']}
        # No automatic create retry: an ambiguous network response must never cause duplicate billing.
        result=self.api('PUT',f"/asks/{offer.get('ask_contract_id') or offer['id']}/",body)
        iid=result.get('new_contract') or result.get('instance_id')
        if not iid: raise RuntimeError('Vast response has no instance ID; inspect Vast console before retrying')
        write_json(FT_DIR/'gpu-instance.json',{'id':iid,'label':label,'actual_status':'provisioning','created_at':utc(),'gpu_name':offer['gpu_name'],'dph_total':offer['dph_total']})
        print(f'Created instance {iid} · {offer["gpu_name"]} · ${offer["dph_total"]}/h',flush=True)
        return iid
    def ssh_args(self,inst):
        host=str(inst.get('ssh_host','')); port=str(inst.get('ssh_port',''))
        if not re.fullmatch(r'[a-zA-Z0-9.-]+',host) or not port.isdigit(): raise RuntimeError('Instance has no valid SSH endpoint yet')
        if not SSH_KEY.is_file(): raise RuntimeError('FT_SSH_KEY is missing')
        return ['-i',str(SSH_KEY),'-o','BatchMode=yes','-o','ConnectTimeout=12','-o','StrictHostKeyChecking=accept-new',
                '-o','UserKnownHostsFile='+str(SSH_KNOWN),'-o','ServerAliveInterval=15','-o','ServerAliveCountMax=3'],host,port
    def ssh(self,inst,command,timeout=45):
        opts,host,port=self.ssh_args(inst)
        try:
            r=subprocess.run(['ssh',*opts,'-p',port,'root@'+host,command],text=True,capture_output=True,timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise CycleFailure('SSH timeout after %ss: %s'%(timeout,command[:90]),'transient','ssh') from exc
        except OSError as exc:
            raise CycleFailure('SSH cannot start: '+redact(exc),'transient','ssh') from exc
        if r.returncode:
            detail=((r.stderr or '')+'\\n'+(r.stdout or '')).strip()[-400:]
            raise CycleFailure('SSH failed (%d): %s'%(r.returncode,redact(detail) or command[:90]),'transient','ssh')
        return r.stdout

    def remote_json(self,inst,path):
        """Read small remote JSON; tolerate missing/truncated files instead of crashing the cycle."""
        content=self.ssh(inst,'cat '+shlex.quote(path)+' 2>/dev/null || true').strip()
        if not content: return {}
        try:
            value=json.loads(content)
            return value if isinstance(value,dict) else {}
        except ValueError:
            start,end=content.find('{'),content.rfind('}')
            while start>=0 and end>start:
                try:
                    value=json.loads(content[start:end+1])
                    if isinstance(value,dict): return value
                except ValueError: pass
                end=content.rfind('}',start,end)
            print('Ignoring malformed remote JSON: '+path,flush=True)
            return {}

    def cache_archive(self,target,source):
        """Rebuild the HF cache tarball only when its source changed."""
        target=Path(target); source=str(Path(source).resolve())
        try:
            fingerprint=source+'\\n'+str(max((p.stat().st_mtime for p in Path(source).rglob('*') if p.is_file()),default=0))
        except OSError:
            fingerprint=source
        marker=target.with_name(target.name+'.source')
        if target.is_file() and target.stat().st_size>1024*1024 and marker.is_file() and marker.read_text(errors='replace')==fingerprint:
            print('Reusing existing model cache archive',flush=True)
            return target
        import tarfile
        with tarfile.open(target,'w:gz',dereference=True) as archive:
            archive.add(source,arcname=Path(source).name)
        marker.write_text(fingerprint)
        return target
    def ready(self):
        while True:
            self.check(); inst=self.tracked()
            if inst:
                self.store(inst)
                if inst.get('actual_status')=='running' and inst.get('ssh_host') and inst.get('ssh_port'):
                    try:
                        info=self.ssh(inst,'nvidia-smi --query-gpu=name,memory.total --format=csv,noheader',timeout=20)
                        print('GPU ready: '+info.strip(),flush=True); return inst
                    except (RuntimeError,subprocess.TimeoutExpired): pass
            time.sleep(5)
    def destroy(self):
        state=read_json(FT_DIR/'gpu-instance.json')
        if not state.get('id'): return {'released':True}
        inst=self.tracked()
        if inst: self.api('DELETE',f"/instances/{state['id']}/")
        write_json(FT_DIR/'gpu-instance.json',{'released_id':state['id'],'released_at':utc()})
        print('Released instance '+str(state['id']),flush=True)
        return {'released':True,'id':state['id']}
    def copy(self,inst,source,dest,download=False,timeout=None):
        opts,host,port=self.ssh_args(inst)
        remote='root@'+host+':'+(source if download else dest)
        argv=['scp',*opts,'-P',port,'-r',remote if download else source,dest if download else remote]
        limit=self.deadline-time.monotonic() if self.deadline else 0
        if timeout: limit=max(limit,timeout)
        try:
            r=subprocess.run(argv,text=True,capture_output=True,timeout=max(1,min(timeout or 300,limit)))
        except subprocess.TimeoutExpired as exc:
            raise CycleFailure('File transfer timeout: '+Path(str(source)).name,'transient','upload') from exc
        if r.returncode:
            raise CycleFailure('File transfer failed: '+redact(r.stderr[-600:]),'transient','upload')

    def upload_chunked(self,inst,source,dest,timeout=1500,chunk_mb=256,per=600,attempts=3):
        """Resumable chunk upload: already-present chunks are skipped, failures retried."""
        opts,host,port=self.ssh_args(inst)
        src=Path(source)
        total=src.stat().st_size
        if not total: raise CycleFailure('Refusing to upload empty file: '+str(src),'fatal','upload')
        try:
            remote_size=self.ssh(inst,'stat -c %s '+shlex.quote(dest)+' 2>/dev/null || echo MISSING').strip()
            if remote_size.isdigit() and int(remote_size)==total:
                print('Remote already has '+dest+' ('+str(total)+' bytes)',flush=True); return
        except CycleFailure:
            pass
        part_dir=Path('/tmp/ft-chunks-'+uuid.uuid4().hex[:8])
        part_dir.mkdir()
        remote_dir='/tmp/ft-cache-chunks'  # stable name: partial upload survives a failed cycle
        complete=False
        try:
            subprocess.run(['split','-b',f'{chunk_mb}m','-d','-a','3',str(src),str(part_dir/'p')],check=True,timeout=600)
            self.ssh(inst,'mkdir -p '+remote_dir)
            listed=self.ssh(inst,'for f in '+remote_dir+'/p*; do [ -e "$f" ] && stat -c "%n %s" "$f"; done 2>/dev/null || true')
            remote_sizes={}
            for line in listed.splitlines():
                parts=line.split()
                if len(parts)==2 and parts[1].isdigit(): remote_sizes[Path(parts[0]).name]=int(parts[1])
            parts=sorted(part_dir.iterdir())
            per=min(per,timeout)
            for i,p in enumerate(parts):
                if remote_sizes.get(p.name)==p.stat().st_size:
                    print('chunk %d/%d already on host'%(i+1,len(parts)),flush=True); continue
                last=None
                for attempt in range(1,attempts+1):
                    try:
                        limit=self.deadline-time.monotonic() if self.deadline else 0
                        t=max(1,min(per,limit)) if limit>0 else per
                        r=subprocess.run(['scp',*opts,'-P',port,str(p),'root@'+host+':'+remote_dir+'/'+p.name],
                                         text=True,capture_output=True,timeout=t)
                        if r.returncode==0: last=None; break
                        last=redact(r.stderr[-300:])
                    except (subprocess.TimeoutExpired,OSError) as exc:
                        last=redact(exc)
                    print('chunk %d/%d retry %d/%d: %s'%(i+1,len(parts),attempt,attempts,str(last)[:140]),flush=True)
                    time.sleep(3*attempt)
                if last is not None:
                    raise CycleFailure('chunk %d/%d of %s failed after %d attempts: %s'%(
                        i+1,len(parts),src.name,attempts,last),'transient','upload')
                print('chunk %d/%d uploaded'%(i+1,len(parts)),flush=True)
            self.ssh(inst,'cat '+remote_dir+'/p* > '+shlex.quote(dest))
            out=self.ssh(inst,'stat -c %s '+shlex.quote(dest))
            got=int(out.strip().splitlines()[-1])
            if got!=total: raise CycleFailure('size mismatch remote=%d local=%d'%(got,total),'transient','upload')
            complete=True
        finally:
            shutil.rmtree(part_dir,ignore_errors=True)
            if complete:
                try: self.ssh(inst,'rm -rf '+remote_dir)
                except Exception: pass

    def collect_dir(self,inst,source,dest,attempts=3):
        """Download a remote directory as a single tarball (resilient to stalls), with retries."""
        import tarfile,shutil
        last=None
        for attempt in range(1,attempts+1):
            try:
                tgz=remote_tgz='/tmp/ft-collect-'+uuid.uuid4().hex[:8]+'.tgz'
                local_tgz=str(Path(dest)/'collect.tgz')
                self.ssh(inst,f'tar czf {tgz} -C {Path(source).parent.as_posix()} {Path(source).name}')
                opts,host,port=self.ssh_args(inst)
                argv=['scp',*opts,'-P',port,'root@'+host+':'+tgz,local_tgz]
                r=subprocess.run(argv,text=True,capture_output=True,timeout=max(60,min(600,self.deadline-time.monotonic())))
                if r.returncode: raise RuntimeError('scp of tarball failed: '+r.stderr[-300:])
                if not (Path(dest)/'collect.tgz').is_file() or (Path(dest)/'collect.tgz').stat().st_size==0:
                    raise RuntimeError('tarball missing or empty')
                with tarfile.open(local_tgz) as tf:
                    tf.extractall(dest)
                (Path(dest)/'collect.tgz').unlink(missing_ok=True)
                return
            except Exception as exc:
                last=exc; print('collect retry',attempt,str(exc)[:200],flush=True); time.sleep(10*attempt)
            finally:
                try: self.ssh(inst,'rm -f '+tgz)
                except Exception: pass
        raise CycleFailure('Collect failed after retries: '+redact(last)[:300],'remote','collect')
    def cycle(self,jid):
        d=job_dir(jid); self.job=d; meta=read_json(d/'job.json'); remote='/workspace/ft-node-'+jid
        if not meta or meta.get('id')!=jid or meta.get('executor')!='vast' or not meta.get('dataset'):
            raise CycleFailure('Job meta missing or stale for %s: cannot train without valid job.json'%jid,'fatal','validating')
        def phase(name):
            write_json(d/'status.json',{'state':'running','phase':name,'updated_at':utc()}); print(name,flush=True)
        phase('resuming')
        inst=None; created=False
        try:
            inst=self.tracked()
            if inst:
                self.store(inst); print('Reusing tracked instance '+str(inst.get('id')),flush=True)
            else:
                phase('provisioning'); self.create(); created=True; inst=self.ready()
            gpu=read_json(d/'gpu.json')
            if not gpu.get('started_at'):
                write_json(d/'gpu.json',{'instance_id':inst.get('id'),'gpu_name':inst.get('gpu_name'),
                                        'dph_total':inst.get('dph_total'),'started_at':utc()})
            phase('uploading')
            self.ssh(inst,'mkdir -p '+remote+'/data/'+jid+' '+remote+'/jobs/'+jid)
            for f in ('gpu_vast.py','trainer.py','worker.py','requirements.txt','ft_node.py'):
                self.copy(inst,str(ROOT/f),remote+'/'+f)
            self.copy(inst,str(FT_DIR/'datasets'/jid)+'/train.jsonl',remote+'/data/'+jid+'/train.jsonl')
            self.copy(inst,str(FT_DIR/'datasets'/jid)+'/test.jsonl',remote+'/data/'+jid+'/test.jsonl')
            remote_meta=self.remote_json(inst,remote+'/jobs/'+jid+'/job.json')
            if remote_meta and remote_meta.get('id')!=jid:
                print('Overwriting stale remote job meta (%s -> %s)'%(remote_meta.get('id'),jid),flush=True)
                log_event(d,'warning','uploading','Replaced stale remote job meta',None)
            self.copy(inst,str(d/'job.json'),remote+'/jobs/'+jid+'/job.json')
            model=meta.get('model') or os.getenv('FT_REMOTE_MODEL','Qwen/Qwen3.5-0.8B-Base')
            offline=''
            src=Path(str(FT_DIR/'.cache/huggingface/hub/models--')+model.replace('/','--'))
            if src.is_dir():
                phase('warming')
                tgz=Path(d)/'model-cache.tgz'
                try:
                    self.cache_archive(tgz,src)
                    self.upload_chunked(inst,str(tgz),'/tmp/ft-cache.tgz')
                    self.ssh(inst,'mkdir -p /root/.cache/huggingface/hub && tar xzf /tmp/ft-cache.tgz -C /root/.cache/huggingface/hub && rm -f /tmp/ft-cache.tgz',timeout=1200)
                    offline=' HF_HUB_OFFLINE=1'
                except CycleFailure as exc:
                    # Cache upload failed but the instance is usable: worker pulls from HF.
                    log_event(d,'warning','warming','Cache upload unavailable, worker will download from HF: '+str(exc),exc)
                    print('Cache upload failed (%s) -> worker downloads model from HF'%str(exc)[:120],flush=True)
                    offline=''
            phase('installing')
            command=f'cd {remote} && python3 -m pip install -r requirements.txt &&{offline} OMP_NUM_THREADS=8 FT_DATA_SUBDIR=data python3 worker.py --root {remote} --model {shlex.quote(model)} --threads 8 --once --executor vast --timeout {int(self.cfg["max_minutes"]*60)}'
            self.ssh(inst,'nohup bash -c '+shlex.quote(command)+' > '+remote+'/setup.log 2>&1 < /dev/null &')
            while True:
                self.check()
                remote_status=self.remote_json(inst,remote+'/jobs/'+jid+'/status.json')
                if remote_status:
                    write_json(d/'status.json',remote_status)
                    state=remote_status.get('state')
                else:
                    state=None
                for name in ('status.json','progress.json','metrics.jsonl','worker.log'):
                    try:
                        content=self.ssh(inst,'test ! -f '+remote+'/jobs/'+jid+'/'+name+' || tail -c 64000 '+remote+'/jobs/'+jid+'/'+name)
                        if content:
                            if name.endswith('.json'):
                                parsed=self.remote_json(inst,remote+'/jobs/'+jid+'/'+name)
                                if parsed: write_json(d/name,parsed)
                            else: (d/name).write_text(content)
                    except (CycleFailure,RuntimeError,subprocess.TimeoutExpired) as exc:
                        print('Telemetry poll warning: '+redact(exc)[:140],flush=True)
                if state in ('completed','failed','cancelled'): break
                setup=self.ssh(inst,'tail -c 6000 '+remote+'/setup.log')
                (d/'setup.log').write_text(setup)
                if 'ERROR:' in setup and not (d/'progress.json').exists():
                    raise CycleFailure('Remote dependency installation failed; see setup.log','remote','installing')
                worker=self.remote_json(inst,remote+'/worker.json')
                stamp=worker.get('updated_at')
                if stamp:
                    try:
                        age=(datetime.now(timezone.utc)-datetime.fromisoformat(stamp)).total_seconds()
                        if age>600:
                            raise CycleFailure('Remote worker heartbeat is %.0fs old; job state stuck at %s'%(age,state),'remote','training')
                    except ValueError:
                        pass
                time.sleep(8)
            if state!='completed':
                detail=read_json(d/'status.json'); message=detail.get('message') or ('remote state '+str(state))
                raise CycleFailure('Remote job %s: %s'%(state,redact(message)[:300]),'remote','training')
            phase('collecting')
            self.collect_dir(inst,remote+'/jobs/'+jid+'/result',str(d))
            if not (d/'result/report.json').is_file() or not (d/'result/adapter/adapter_model.safetensors').is_file():
                raise CycleFailure('Returned result is incomplete','remote','collect')
            try:
                from datetime import datetime as _dt, timezone as _tz
                g=read_json(d/'gpu.json')
                hours=(_dt.now(_tz.utc)-_dt.fromisoformat(g['started_at'])).total_seconds()/3600
                g.update({'ended_at':utc(),'hours':round(hours,3),'cost_usd':round(hours*float(g.get('dph_total') or 0),3)})
                write_json(d/'gpu.json',g)
            except (OSError,ValueError,KeyError): pass
            write_json(d/'status.json',{'state':'completed','phase':'done','updated_at':utc()})
            try: self.destroy()
            except Exception as exc: print('Could not release instance after success: '+redact(exc)[:200],flush=True)
            return {'job':jid,'collected':True}
        except Exception as exc:
            record_failure(d,exc)
            category,_=failure_parts(exc)
            # Salvage BEFORE any destructive decision, even for a non-created instance.
            if inst:
                try:
                    for name in ('setup.log','worker.log'):
                        try:
                            content=self.ssh(inst,'test ! -f '+remote+'/'+name+' || tail -c 128000 '+remote+'/'+name)
                            (d/name).write_text(content or '')
                        except Exception: pass
                    if not (d/'result'/'adapter'/'adapter_model.safetensors').exists():
                        status=read_json(d/'status.json'); status['phase']='salvage'; write_json(d/'status.json',status)
                        self.collect_dir(inst,remote+'/jobs/'+jid+'/result',str(d))
                        print('Salvaged remote result after error',flush=True)
                except Exception as salvage_exc:
                    print('Salvage failed: '+redact(salvage_exc)[:300],flush=True)
            # A transient failure keeps the instance: the next turn resumes instead of
            # losing partially uploaded chunks or paying for a new host.
            if category in ('remote','cancelled','fatal') and inst:
                try: self.destroy()
                except Exception as destroy_exc:
                    print('Release after failure failed: '+redact(destroy_exc)[:200],flush=True)
            else:
                print('Instance retained for retry (category=%s)'%category,flush=True)
            raise


def main():
    init(); p=argparse.ArgumentParser(); p.add_argument('action',choices=['search','status','health','up','down','cycle']); p.add_argument('--operation'); p.add_argument('--job'); args=p.parse_args()
    op=FT_DIR/'operations'/(args.operation+'.json') if args.operation else None
    # OS lock also protects CLI use alongside the portal.
    with (FT_DIR/'gpu.lock').open('a') as lock:
        try:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            v=Vast(); result={}
            if args.action=='search': result={'offers':v.offers()}
            elif args.action=='status':
                inst=v.tracked()
                if inst: v.store(inst)
                result={'instance':read_json(FT_DIR/'gpu-instance.json'),'found':bool(inst)}
            elif args.action=='health':
                inst=v.tracked()
                if not inst: raise ValueError('No tracked instance')
                result={'gpu':v.ssh(inst,'nvidia-smi'), 'python':v.ssh(inst,"python3 -c 'import torch; print(torch.__version__, torch.cuda.is_available())'")}
            elif args.action=='up':
                created=False
                try:
                    if not read_json(FT_DIR/'gpu-instance.json').get('id'): v.create(); created=True
                    v.ready(); result={'ready':True}
                except Exception:
                    if created: v.destroy()
                    raise
            elif args.action=='down': result=v.destroy()
            elif args.action=='cycle': result=v.cycle(args.job)
            print(json.dumps(result,indent=2),flush=True)
            if op: write_json(op,{**read_json(op),'state':'completed','result':result,'finished_at':utc()})
        except Exception as exc:
            # No provider key, bearer token or raw request URL in portal logs.
            message=redact(exc)
            category,phase=(exc.category,exc.phase) if isinstance(exc,CycleFailure) else ('fatal',None)
            # Blacklist only a fatal provider-level rejection; never for network blips
            # or for failures after the instance was being used.
            if args.action=='cycle' and category=='fatal' and phase=='provider':
                try:
                    last=read_json(FT_DIR/'gpu-last-offer.json').get('contract')
                    if last:
                        bl=read_json(FT_DIR/'gpu-blacklist.json').get('contracts',[])
                        if str(last) not in {str(x) for x in bl}:
                            bl.append(last); write_json(FT_DIR/'gpu-blacklist.json',{'contracts':bl[-10:]})
                except Exception: pass
            print('ERROR: '+message,flush=True)
            if op: write_json(op,{**read_json(op),'state':'failed','error':message,'error_category':category,'finished_at':utc()})
            return 1
    return 0
if __name__=='__main__': sys.exit(main())
