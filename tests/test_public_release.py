import copy
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime,timedelta
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/"src"),str(ROOT/"console")]
import api
import settings
import configuration
import posting_policy
import tieba_post
import browser_service
import repost_control

class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        for module in [settings,api,configuration]:
            p=patch.object(module,"ROOT",self.root);p.start();self.addCleanup(p.stop)
        p=patch.object(api,"LOCAL_PROFILES",self.root/"state"/"api-profiles.json");p.start();self.addCleanup(p.stop)
        p=patch.object(api,"LOCAL_KEY_FILE",self.root/"state"/"api-key.txt");p.start();self.addCleanup(p.stop)

    def test_safe_first_start_and_independent_config(self):
        cfg=settings.load();self.assertFalse(cfg['auto']['enabled']);self.assertFalse(cfg['repost']['enabled']);self.assertEqual(cfg['forums'],[])
        settings.save(cfg);self.assertEqual(settings.load(),cfg)

    def test_profiles_keep_secret_on_blank_update_and_never_return_it(self):
        body={'id':'primary','endpoint':'https://example.test/v1','model':'test-model','apiKey':'synthetic-only-credential'}
        r=api.save_profile(body);self.assertNotIn('synthetic-only-credential',json.dumps(r));self.assertTrue(r['profiles'][0]['hasKey'])
        api.save_profile({**body,'apiKey':''});cfg=settings.load();cfg['api']['profile']='primary'
        self.assertEqual(api.resolve(cfg)['key'],'synthetic-only-credential')

    def test_no_implicit_shared_qq_project_or_fallback_from_deleted_profile(self):
        cfg=settings.load();cfg['api']['profile']='missing'
        with self.assertRaisesRegex(ValueError,'不存在'):api.resolve(cfg)
        with patch.dict(os.environ,{'TIEBA_AGENT_API_KEY':'environment-fixture'}):
            cfg['api']['profile']='';self.assertEqual(api.resolve(cfg)['key'],'environment-fixture')

    def test_endpoint_paths_are_not_duplicated(self):
        for url,want in [('https://example.test','https://example.test/v1/chat/completions'),('https://example.test/v1','https://example.test/v1/chat/completions'),('https://example.test/v1beta/openai','https://example.test/v1beta/openai/chat/completions')]:self.assertEqual(api.chat_url(url),want)
        for url in ['file:///tmp/key','https://user:secret@example.test','https://example.test?key=test']:
            with self.assertRaises(ValueError):api.validate_endpoint(url)

    def test_vision_can_inherit_reply_api_or_select_an_independent_one(self):
        api.save_profile({'id':'reply','endpoint':'https://reply.test/v1','model':'reply-model','apiKey':'reply-fixture'})
        api.save_profile({'id':'vision','endpoint':'https://vision.test/v1','model':'vision-model','apiKey':'vision-fixture'})
        cfg=settings.load();cfg['api']['profile']='reply';settings.save(cfg)
        self.assertEqual(api.resolve_vision({})['key'],'reply-fixture')
        self.assertEqual(api.resolve_vision({'profile':'vision'})['model'],'vision-model')
        self.assertEqual(api.resolve_vision({'profile':'vision','model':'overridden'})['model'],'overridden')

    def test_all_repost_source_settings_and_api_choices_survive_save(self):
        cfg=settings.load();cfg['repost'].update({'forums':['源甲'],'sourceForums':['源甲','源乙'],'sourceMode':'sitewide','target':'目标甲','enabled':True,'judgeProfile':'judge','judgeWithModel':False,'altTarget':'目标乙','altEvery':3})
        configuration.configure(cfg);self.assertEqual(settings.load()['repost'],cfg['repost'])

    def test_invalid_schedule_and_secret_in_config_are_rejected(self):
        for change in [{'activeHours':['25:00-26:00']},{'pollSeconds':-1},{'apiKey':'never-store-here'},{'persona':'../outside.md'},{'repost':{'enabled':True}}]:
            with self.assertRaises((ValueError,KeyError)):settings.save(settings.merge(settings.load(),change))

    def test_times_cross_midnight_and_reply_quotas_use_config_values(self):
        cfg=settings.load();cfg['activeHours']=['22:00-02:00'];now=datetime(2026,1,2,1,0)
        self.assertTrue(posting_policy.in_hours(cfg['activeHours'],now));self.assertFalse(posting_policy.in_hours(cfg['activeHours'],now.replace(hour=12)))
        cfg['minMinutesBetween']=45
        rows=[{'at':(now-timedelta(minutes=20)).isoformat()}]
        self.assertEqual(posting_policy.check(cfg,rows,now),'未达到回复最小间隔')
        cfg['minMinutesBetween']=5;self.assertEqual(posting_policy.check(cfg,rows,now),'')
        cfg['maxRepliesPerHour']=1;self.assertEqual(posting_policy.check(cfg,rows,now),'本小时回复已达到上限')

    def test_statistics_parse_publication_times_in_operator_timezone(self):
        from zoneinfo import ZoneInfo
        from repost_stats import post_time
        stamp=post_time('2026-11-24 12:00',tz=ZoneInfo('Asia/Shanghai'))
        self.assertEqual(stamp,datetime(2026,11,24,4,0,tzinfo=ZoneInfo('UTC')).timestamp())

    def test_publishing_respects_target_and_reserves_quota_before_network(self):
        cfg=settings.load();cfg.update({'forums':['allowed'],'activeHours':['00:00-23:59'],'minMinutesBetween':0,'maxRepliesPerHour':1})
        calls=[]
        def send(*args,**kwargs):
            calls.append(args);self.assertEqual(posting_policy._ledger()[-1]['status'],'pending');return {'ok':True}
        with patch.object(tieba_post,'reply',send):
            self.assertFalse(posting_policy.publish(cfg,1,'fixture','forbidden')['ok']);self.assertEqual(calls,[])
            self.assertTrue(posting_policy.publish(cfg,1,'fixture','allowed')['ok'])
            self.assertFalse(posting_policy.publish(cfg,2,'fixture','allowed')['ok']);self.assertEqual(len(calls),1)

    def test_account_api_never_returns_login_cookie(self):
        cookie='synthetic_bduss_fixture_1234567890'
        r=configuration.account({'bduss':cookie});self.assertNotIn(cookie,json.dumps(r));self.assertTrue(r['accountConfigured'])
        self.assertEqual(json.loads((self.root/'state'/'auth.json').read_text())['bduss'],cookie)

    def test_browser_is_lazy_and_rejects_foreign_login_urls(self):
        browser=browser_service.Browser();self.assertEqual(browser.command('health',{}),{'running':True,'browserOpen':False})
        for url in ['file:///private','https://baidu.com.evil.test/','http://tieba.baidu.com/']:
            with self.assertRaises(ValueError):browser_service.checked_url(url)

    def test_manual_start_stop_is_persisted_and_cancels_pending_work(self):
        cfg=settings.load();cfg['repost'].update({'forums':['源吧'],'target':'目标吧','judgeWithModel':False})
        settings.save(cfg);configuration.account({'bduss':'synthetic_account_fixture_12345'})
        with patch.object(configuration,'ensure_repost_worker',return_value=True) as start:
            result=configuration.repost_control({'action':'start'})
            self.assertTrue(result['enabled']);start.assert_called_once();self.assertFalse(repost_control.stopped())
        flag=self.root/'state'/'repost-now.flag';flag.write_text('queued')
        result=configuration.repost_control({'action':'stop'})
        self.assertFalse(result['enabled']);self.assertTrue(repost_control.stopped());self.assertFalse(flag.exists())
        self.assertEqual(settings.load()['repost']['target'],'目标吧');self.assertFalse(settings.load()['auto']['enabled'])
        self.assertFalse(configuration.repost_control({'action':'stop'})['enabled'])

    def test_stopping_blocks_buttons_shortcuts_and_story_fallback_before_network(self):
        import repost
        import tieba_browser
        cfg=settings.load();settings.save(cfg)
        with patch.object(repost,'CONFIG',self.root/'config.json'),patch.object(tieba_browser,'_api') as browser,patch.object(tieba_post,'reply') as reply:
            for action,args in [('evaluate',{'code':repost.PUBLISH_JS}),('evaluate',{'code':repost.REPLY_PUBLISH_JS}),('send_keys',{'keys':'Ctrl+Enter'})]:
                with self.assertRaises(repost_control.RepostStopped):repost._bridge(action,args)
            self.assertEqual(repost.post_item({})['stage'],'stopped')
            repost.post_extra_parts({'extra_parts':[{'text':'not-published'}]},'https://tieba.baidu.com/p/123')
            browser.assert_not_called();reply.assert_not_called()

    def test_server_import_does_not_start_workers(self):
        spec=importlib.util.spec_from_file_location('release_console',ROOT/'console'/'server.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
        self.assertFalse(m.SCHED.is_alive());self.assertEqual(m.load_cfg()['auto']['enabled'],False)

if __name__=='__main__':unittest.main()
