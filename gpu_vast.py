#!/usr/bin/env python3
"""Vast.ai FineTune executor: provision → SSH → train → collect → destroy.
Secrets stay on the controller. Only a tracked instance with our exact label is mutable.
"""
import argparse
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

API='https://console.vast.ai/api/v0'
KEY_PATH=Path(os.getenv('FT_VAST_KEY','/etc/ft-node/vast_api_key'))
SSH_KEY=Path(os.getenv('FT_SSH_KEY','/etc/ft-node/id_ed25519'))
SSH_KNOWN=FT_DIR/'known_hosts'

class Vast:
    def __init__(self):
        self.key=os.getenv('VAST_API_KEY') or (KEY_PATH.read_text().strip() if KEY_PATH.is_file() else '')
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
                if exc.code!=429 or attempt==5: raise RuntimeError(f'Vast API returned HTTP {exc.code}') from None
                time.sleep(min(60, 5*(attempt+1)))
            except urllib.error.URLError as exc:
                if attempt==5: raise RuntimeError('Vast API unreachable: '+str(exc.reason)) from None
                time.sleep(min(60, 5*(attempt+1)))
        if isinstance(result,dict) and (result.get('success') is False or result.get('error')):
            raise RuntimeError('Vast API rejected request: '+str(result.get('msg',result.get('error')))[:200])
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
        r=subprocess.run(['ssh',*opts,'-p',port,'root@'+host,command],text=True,capture_output=True,timeout=timeout)
        if r.returncode: raise RuntimeError('SSH: '+r.stderr[-600:])
        return r.stdout
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
    def copy(self,inst,source,dest,download=False):
        opts,host,port=self.ssh_args(inst)
        remote='root@'+host+':'+(source if download else dest)
        argv=['scp',*opts,'-P',port,'-r',remote if download else source,dest if download else remote]
        r=subprocess.run(argv,text=True,capture_output=True,timeout=max(1,min(300,self.deadline-time.monotonic())))
        if r.returncode: raise RuntimeError('File transfer failed: '+r.stderr[-600:])
    def cycle(self,jid):
        d=job_dir(jid); self.job=d; meta=read_json(d/'job.json'); remote='/workspace/ft-node-'+jid
        def phase(name):
            write_json(d/'status.json',{'state':'running','phase':name,'updated_at':utc()}); print(name,flush=True)
        created=False
        try:
            phase('provisioning'); self.create(); created=True; inst=self.ready()
            write_json(d/'gpu.json',{'instance_id':inst.get('id'),'gpu_name':inst.get('gpu_name'),'dph_total':inst.get('dph_total'),'started_at':utc()})
            phase('uploading'); self.ssh(inst,'mkdir -p '+remote+'/data/'+jid+' '+remote+'/jobs/'+jid)
            for f in ('trainer.py','worker.py','requirements.txt'):
                self.copy(inst,str(ROOT/f),remote+'/'+f)
            self.copy(inst,str(FT_DIR/'datasets'/jid)+'/train.jsonl',remote+'/data/'+jid+'/train.jsonl')
            self.copy(inst,str(FT_DIR/'datasets'/jid)+'/test.jsonl',remote+'/data/'+jid+'/test.jsonl')
            self.copy(inst,str(d/'job.json'),remote+'/jobs/'+jid+'/job.json')
            phase('installing')
            # SSH launches a remote process; controller polls without tying up an HTTP request.
            model=os.getenv('FT_REMOTE_MODEL','Qwen/Qwen3.5-0.8B-Base')
            command=f'cd {remote} && python3 -m pip install -r requirements.txt && OMP_NUM_THREADS=8 FT_DATA_SUBDIR=data python3 worker.py --root {remote} --model {shlex.quote(model)} --threads 8 --once --executor vast --timeout {int(self.cfg["max_minutes"]*60)}'
            self.ssh(inst,'nohup bash -c '+shlex.quote(command)+' > '+remote+'/setup.log 2>&1 < /dev/null &')
            while True:
                self.check()
                # Poll only small telemetry files. Artifact copy happens once at completion.
                for name in ('status.json','progress.json','metrics.jsonl','worker.log'):
                    try:
                        content=self.ssh(inst,'test ! -f '+remote+'/jobs/'+jid+'/'+name+' || tail -c 64000 '+remote+'/jobs/'+jid+'/'+name)
                        if content:
                            if name.endswith('.json'): write_json(d/name,json.loads(content))
                            else: (d/name).write_text(content)
                    except (RuntimeError,ValueError,subprocess.TimeoutExpired): pass
                state=read_json(d/'status.json').get('state')
                if state in ('completed','failed','cancelled'): break
                setup=self.ssh(inst,'tail -c 6000 '+remote+'/setup.log')
                (d/'setup.log').write_text(setup)
                if 'ERROR:' in setup and not (d/'progress.json').exists(): raise RuntimeError('Remote dependency installation failed; see setup.log')
                time.sleep(8)
            if state!='completed': raise RuntimeError('Remote job '+str(state))
            phase('collecting')
            self.copy(inst,remote+'/jobs/'+jid+'/result',str(d),download=True)
            if not (d/'result/report.json').is_file() or not (d/'result/adapter/adapter_model.safetensors').is_file(): raise RuntimeError('Returned result is incomplete')
            try:
                from datetime import datetime as _dt, timezone as _tz
                g=read_json(d/'gpu.json')
                hours=(_dt.now(_tz.utc)-_dt.fromisoformat(g['started_at'])).total_seconds()/3600
                g.update({'ended_at':utc(),'hours':round(hours,3),'cost_usd':round(hours*float(g.get('dph_total') or 0),3)})
                write_json(d/'gpu.json',g)
            except (OSError,ValueError,KeyError): pass
            write_json(d/'status.json',{'state':'completed','phase':'done','updated_at':utc()})
            return {'job':jid,'collected':True}
        except Exception as exc:
            write_json(d/'status.json',{'state':'cancelled' if isinstance(exc,InterruptedError) else 'failed','phase':'error','message':str(exc),'updated_at':utc()})
            raise
        finally:
            # Includes failures after create but before readiness. Retain instance ID if deletion fails.
            if created:
                self.destroy()

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
            message=str(exc); secret=os.getenv('VAST_API_KEY') or (KEY_PATH.read_text().strip() if KEY_PATH.is_file() else '')
            if secret: message=message.replace(secret,'[redacted]')
            # A failure before the dataset upload means the host never became usable: blacklist it.
            logtext=''
            try: logtext=(FT_DIR/'operations'/(args.operation+'.log')).read_text() if args.operation else ''
            except OSError: pass
            if 'uploading' not in logtext:
                try:
                    last=read_json(FT_DIR/'gpu-last-offer.json').get('contract')
                    if last:
                        bl=read_json(FT_DIR/'gpu-blacklist.json').get('contracts',[])
                        if str(last) not in {str(x) for x in bl}:
                            bl.append(last); write_json(FT_DIR/'gpu-blacklist.json',{'contracts':bl[-10:]})
                except Exception: pass
            print('ERROR: '+message,flush=True)
            if op: write_json(op,{**read_json(op),'state':'failed','error':message,'finished_at':utc()})
            return 1
    return 0
if __name__=='__main__': sys.exit(main())
