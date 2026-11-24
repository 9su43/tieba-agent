"""Fresh export smoke test; automation off, synthetic credentials, no external posting."""
import copy
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]

def free_port():
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));return sock.getsockname()[1]

class FreshConsole(unittest.TestCase):
    def test_new_operator_can_configure_every_module_without_old_machine(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for folder in ['src','console','persona','tools']:shutil.copytree(ROOT/folder,root/folder,ignore=shutil.ignore_patterns('__pycache__'))
            cfg=json.loads((ROOT/'config.example.json').read_text(encoding='utf-8'));port=free_port();cfg['console']['port']=port;cfg['browser']['port']=free_port()
            (root/'config.json').write_text(json.dumps(cfg),encoding='utf-8')
            log=(root/'startup.log').open('w',encoding='utf-8')
            child=subprocess.Popen([sys.executable,'-X','utf8','console/server.py'],cwd=root,stdout=log,stderr=log,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            try:
                tokenfile=root/'state'/'console-token.txt'
                for _ in range(100):
                    if tokenfile.exists():break
                    if child.poll() is not None:raise AssertionError('Console did not start')
                    time.sleep(.05)
                token=tokenfile.read_text().strip()
                def request(path,body=None,auth=True):
                    req=urllib.request.Request(f'http://127.0.0.1:{port}'+path,data=None if body is None else json.dumps(body).encode(),headers={'Authorization':'Bearer '+(token if auth else 'invalid'),'Content-Type':'application/json'})
                    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req,timeout=5) as r:return r.read()
                with self.assertRaises(urllib.error.HTTPError):request('/api/status',auth=False)
                status=json.loads(request('/api/status'));self.assertFalse(status['config']['auto']['enabled']);self.assertEqual(status['config']['forums'],[])
                self.assertFalse(json.loads(request('/api/repost/control',{'action':'stop'}))['enabled'])
                with self.assertRaises(urllib.error.HTTPError):request('/api/repost/run',{})
                credential='http-test-fixture-only'
                request('/api/profiles',{'id':'reply','endpoint':'https://example.test/v1','model':'fixture','apiKey':credential})
                cfg['forums']=['源甲'];cfg['api']['profile']='reply';cfg['repost'].update({'forums':['源乙'],'sourceForums':['源丙'],'sourceMode':'sitewide','target':'目标甲','perDay':7,'intervalMinutes':17,'judgeProfile':'reply'})
                cfg['activeHours']=['23:00-01:00'];request('/api/settings',cfg)
                data=request('/api/settings');self.assertNotIn(credential.encode(),data)
                view=json.loads(data);self.assertEqual(view['config']['repost']['sourceForums'],['源丙']);self.assertEqual(view['config']['repost']['intervalMinutes'],17);self.assertEqual(view['config']['api']['profile'],'reply')
                self.assertIn('完整设置'.encode('utf-8'),request('/settings'))
                self.assertEqual(request('/api/persona')[:1],b'{')
                self.assertFalse((root/'state'/'auth.json').exists())
                # CLI dry-run in the same clean export with no source forums, hence no external requests.
                cfg['forums']=[];(root/'config.json').write_text(json.dumps(cfg),encoding='utf-8')
                result=subprocess.run([sys.executable,'-X','utf8','src/main.py','--once'],cwd=root,capture_output=True,text=True,encoding='utf-8',timeout=15)
                self.assertEqual(result.returncode,0,result.stderr)
            finally:
                child.terminate()
                try:child.wait(timeout=8)
                except subprocess.TimeoutExpired:child.kill();child.wait(timeout=3)
                log.close()
