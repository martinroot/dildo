import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import ft_node as app
import gpu_vast
from trainer import label

class PortalTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.old=app.FT_DIR; app.FT_DIR=Path(self.tmp.name); app.init()
    def tearDown(self): app.FT_DIR=self.old; self.tmp.cleanup()
    def pair(self,date='2026-01-01T00:00:00+00:00'):
        return label({'as_of':date,'slices':[{'slice_id':'1290:BR','clicks':300,'conversions':4,'profit_cents':70,'postbacks_mature':True}]})
    def dataset(self):
        return app.import_dataset({'name':'test-v1','train':json.dumps(self.pair()),'test':json.dumps(self.pair('2026-01-02T00:00:00+00:00'))})
    def test_validation_and_leakage(self):
        p=json.dumps(self.pair()); r=app.validate_text(p,p)
        self.assertFalse(r['ok']); self.assertEqual(r['overlap'],1)
        bad=self.pair(); bad['output'][0]['reason']='loss'
        self.assertFalse(app.validate_text(json.dumps(bad),p)['ok'])
        self.assertFalse(app.validate_text('', '')['ok'])
        self.assertFalse(app.validate_text(p+'\n'+p,json.dumps(self.pair('2026-01-02T00:00:00+00:00')))['ok'])
    def test_import_immutable_and_queue_snapshot(self):
        self.dataset()
        with self.assertRaises(ValueError): self.dataset()
        job=app.queue_job({'dataset':'test-v1','preset':'smoke','device':'cpu'})
        self.assertTrue((app.FT_DIR/'datasets'/job['id']/'train.jsonl').is_file())
        self.assertFalse((app.FT_DIR/'datasets'/job['id']).is_symlink())
        (app.FT_DIR/'datasets/test-v1/train.jsonl').write_text('changed')
        self.assertNotEqual((app.FT_DIR/'datasets'/job['id']/'train.jsonl').read_text(),'changed')
        with self.assertRaises(ValueError): app.queue_job({'dataset':'test-v1','preset':'smoke','device':'invalid'})
    def test_release_gates_enforced_and_artifact_required(self):
        self.dataset(); j=app.queue_job({'dataset':'test-v1'}); d=app.job_dir(j['id'])
        app.write_json(d/'status.json',{'state':'completed'})
        m={'examples':4,'valid_json_and_schema':4,'exact_slices':4,'total_slices':4}
        app.write_json(d/'result/report.json',{'base':m,'lora':m})
        with self.assertRaisesRegex(ValueError,'Release blocked'): app.activate({'job':j['id']})
        m.update(examples=16,valid_json_and_schema=16,exact_slices=16,total_slices=16)
        app.write_json(d/'result/report.json',{'base':m,'lora':m})
        with self.assertRaisesRegex(ValueError,'artifact'): app.activate({'job':j['id']})
        (d/'result/adapter').mkdir(); (d/'result/adapter/adapter_model.safetensors').write_bytes(b'test fixture')
        self.assertEqual(app.activate({'job':j['id']})['active'],j['id'])
        app.save_settings({'criteria':{'min_examples':20}})
        with self.assertRaisesRegex(ValueError,'Release blocked'): app.activate({'job':j['id']})
    def test_settings_reject_nan_and_invalid_types(self):
        for p in [{'criteria':{'min_accuracy':float('nan')}},{'gpu':{'max_dph':-1}},{'criteria':{'no_regression':'yes'}}]:
            with self.assertRaises(ValueError): app.save_settings(p)
    def test_stale_worker_is_offline(self):
        app.write_json(app.FT_DIR/'worker.json',{'updated_at':'2020-01-01T00:00:00+00:00'})
        self.assertFalse(app.status_view()['worker']['online'])
        app.write_json(app.FT_DIR/'worker.json',{'updated_at':app.utc()})
        self.assertTrue(app.status_view()['worker']['online'])
    def test_cancel_pending(self):
        self.dataset(); j=app.queue_job({'dataset':'test-v1'}); app.cancel({'job':j['id']})
        self.assertEqual(app.read_json(app.job_dir(j['id'])/'status.json')['state'],'cancelled')
    def test_structured_metrics(self):
        self.dataset(); j=app.queue_job({'dataset':'test-v1'}); d=app.job_dir(j['id'])
        (d/'metrics.jsonl').write_text('{"step":10,"loss":0.25}\npartial')
        self.assertEqual(app.progress_view(j['id'])['loss_series'],[{'step':10,'loss':.25}])
    def test_http_routes_assets_methods_and_auth(self):
        srv=app.ThreadingHTTPServer(('127.0.0.1',0),app.Handler); threading.Thread(target=srv.serve_forever,daemon=True).start()
        base='http://127.0.0.1:'+str(srv.server_port)
        try:
            with urllib.request.urlopen(base+'/') as r: self.assertIn(b'FineTune Workspace',r.read())
            with urllib.request.urlopen(base+'/assets/portal.css') as r: self.assertEqual(r.status,200)
            with urllib.request.urlopen(base+'/status') as r: self.assertIn('jobs',json.load(r))
            req=urllib.request.Request(base+'/settings',data=b'[]',headers={'Content-Type':'application/json'})
            with self.assertRaises(urllib.error.HTTPError) as exc: urllib.request.urlopen(req)
            self.assertEqual(exc.exception.code,422)
            with patch.dict(os.environ,{'FT_API_TOKEN':'secret'}):
                with self.assertRaises(urllib.error.HTTPError) as exc: urllib.request.urlopen(base+'/status')
                self.assertEqual(exc.exception.code,401)
                req=urllib.request.Request(base+'/status',headers={'Authorization':'Bearer secret'})
                with urllib.request.urlopen(req) as r: self.assertEqual(r.status,200)
                req=urllib.request.Request(base+'/status',headers={'X-FT-Authenticated':'1'})
                with urllib.request.urlopen(req) as r: self.assertEqual(r.status,200)
                req=urllib.request.Request(base+'/settings',data=b'{}',headers={'Content-Type':'application/json','X-FT-Authenticated':'1','Origin':'https://foreign.invalid'})
                with self.assertRaises(urllib.error.HTTPError) as exc: urllib.request.urlopen(req)
                self.assertEqual(exc.exception.code,403)
        finally: srv.shutdown();srv.server_close()
    def test_gpu_failure_releases_only_created_instance(self):
        self.dataset(); j=app.queue_job({'dataset':'test-v1','executor':'vast'})
        with patch.dict(os.environ,{'VAST_API_KEY':'test'}): v=gpu_vast.Vast()
        with patch.object(v,'create',return_value=123),patch.object(v,'ready',side_effect=TimeoutError('not ready')),patch.object(v,'destroy') as destroy:
            with self.assertRaises(TimeoutError): v.cycle(j['id'])
            destroy.assert_called_once()
        with patch.object(v,'create',side_effect=ValueError('tracked instance already exists')),patch.object(v,'destroy') as destroy:
            with self.assertRaises(ValueError): v.cycle(j['id'])
            destroy.assert_not_called()
    def test_gpu_collects_before_release(self):
        self.dataset(); j=app.queue_job({'dataset':'test-v1','executor':'vast'}); d=app.job_dir(j['id'])
        events=[]
        with patch.dict(os.environ,{'VAST_API_KEY':'test'}): v=gpu_vast.Vast()
        def fake_ssh(inst,command,**kwargs):
            if '/status.json' in command: return json.dumps({'state':'completed','phase':'done'})
            return ''
        def fake_copy(inst,source,dest,download=False):
            if download:
                events.append('collect')
                app.write_json(d/'result/report.json',{'model':'fixture'})
                (d/'result/adapter').mkdir(); (d/'result/adapter/adapter_model.safetensors').write_bytes(b'fixture')
        with patch.object(v,'create',return_value=123),patch.object(v,'ready',return_value={}),patch.object(v,'ssh',side_effect=fake_ssh),patch.object(v,'copy',side_effect=fake_copy),patch.object(v,'destroy',side_effect=lambda:events.append('release')):
            result=v.cycle(j['id'])
        self.assertTrue(result['collected'])
        self.assertEqual(events,['collect','release'])
        self.assertEqual(app.read_json(d/'status.json')['state'],'completed')

    def test_gpu_search_uses_authenticated_get_query(self):
        from urllib.parse import urlsplit, parse_qs
        with patch.dict(os.environ,{'VAST_API_KEY':'test'}),patch.object(gpu_vast,'FT_DIR',app.FT_DIR):
            v=gpu_vast.Vast()
            with patch.object(v,'api',return_value={'offers':[{'id':1,'gpu_name':'RTX 4090','gpu_ram':24000,'dph_total':.4},{'id':2,'dph_total':9}]}) as api:
                offers=v.offers()
                self.assertEqual(len(offers),1)
                method,path=api.call_args.args
                self.assertEqual(method,'GET')
                query=json.loads(parse_qs(urlsplit(path).query)['q'][0])
                self.assertEqual(query['gpu_ram']['gte'],16000)
                self.assertNotIn('api_key',path)

    def test_adapter_zip_download(self):
        import io, zipfile
        self.dataset(); j=app.queue_job({'dataset':'test-v1'}); d=app.job_dir(j['id'])
        app.write_json(d/'status.json',{'state':'completed'})
        app.write_json(d/'result/report.json',{'model':'test-fixture'})
        (d/'result/adapter').mkdir(); (d/'result/adapter/adapter_model.safetensors').write_bytes(b'fixture')
        srv=app.ThreadingHTTPServer(('127.0.0.1',0),app.Handler); threading.Thread(target=srv.serve_forever,daemon=True).start()
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{srv.server_port}/artifacts/'+j['id']) as r:
                self.assertEqual(r.headers['Content-Type'],'application/zip')
                with zipfile.ZipFile(io.BytesIO(r.read())) as archive:
                    self.assertEqual(archive.read('adapter/adapter_model.safetensors'),b'fixture')
                    self.assertIn('job.json',archive.namelist())
                    self.assertIn('report.json',archive.namelist())
        finally: srv.shutdown();srv.server_close()

    def test_gpu_owned_instance_exact_label(self):
        with patch.dict(os.environ,{'VAST_API_KEY':'test'}),patch.object(gpu_vast,'FT_DIR',app.FT_DIR):
            v=gpu_vast.Vast(); app.write_json(app.FT_DIR/'gpu-instance.json',{'id':1,'label':'ft-node-owned'})
            with patch.object(v,'instances',return_value=[{'id':1,'label':'unrelated'}]):
                with self.assertRaisesRegex(RuntimeError,'ownership'): v.tracked()

if __name__=='__main__': unittest.main()
