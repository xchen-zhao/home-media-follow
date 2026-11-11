"""Hourly discovery timing and the existing cloud/local authorization boundary."""
import copy
from datetime import datetime,timedelta,timezone
import unittest
from home_media_follow.worker import Worker
from home_media_follow.core import logical_key
import test_worker_experience as fixtures

CST=timezone(timedelta(hours=8))
def stamp(day,hour,minute=0):return datetime(2026,11,day,hour,minute,tzinfo=CST).timestamp()

class HourlyTimingTests(unittest.TestCase):
    def setUp(self):
        self.w=Worker.__new__(Worker)
        self.w.config={'scan_interval_seconds':3600,'recovery_interval_seconds':900,'scan_hour':18}
        self.w.clock=lambda:stamp(9,19)
    def test_after_18_finds_new_hour_even_with_todays_daily_marker(self):
        self.assertTrue(self.w._scan_due({'last_daily_scan':'2026-11-09','last_scan_at':stamp(9,18,7),'last_scan_attempt':stamp(9,18)}))
    def test_quarter_hour_recovery_does_not_repeat_successful_hour(self):
        for minute in (5,20,35,50):
            self.w.clock=lambda m=minute:stamp(9,19,m)
            self.assertFalse(self.w._scan_due({'last_scan_at':stamp(9,19,1),'last_scan_attempt':stamp(9,19)}))
    def test_next_hour_and_midnight_are_due(self):
        for previous,current in ((stamp(9,19,5),stamp(9,20)),(stamp(9,23,5),stamp(10,0))):
            self.w.clock=lambda:current
            self.assertTrue(self.w._scan_due({'last_scan_at':previous,'last_scan_attempt':previous}))
    def test_resume_after_missing_several_hours_runs_one_check(self):
        self.assertTrue(self.w._scan_due({'last_scan_at':stamp(9,9),'last_scan_attempt':stamp(9,9)}))
    def test_success_just_before_hour_does_not_apply_failed_attempt_backoff(self):
        self.w.clock=lambda:stamp(9,20)
        self.assertTrue(self.w._scan_due({'last_scan_at':stamp(9,19,59),'last_scan_attempt':stamp(9,19,58)}))
    def test_recent_failed_attempt_waits_for_recovery_backoff(self):
        sub={'last_scan_at':stamp(9,17),'last_scan_attempt':stamp(9,18,55)}
        self.assertFalse(self.w._scan_due(sub))
        self.w.clock=lambda:stamp(9,19,10)
        self.assertTrue(self.w._scan_due(sub))
    def test_first_scan_and_explicit_force(self):
        self.assertTrue(self.w._scan_due({}))
        self.assertTrue(self.w._scan_due({'last_scan_at':stamp(9,19)},True))
    def test_invalid_intervals_fail_closed(self):
        for value in (True,0,60,'3600',86401):
            self.w.config['scan_interval_seconds']=value
            with self.assertRaises(ValueError):self.w._scan_due({})
    def test_legacy_daily_configuration_retains_missed_day_behavior(self):
        self.w.config['scan_interval_seconds']=86400
        self.w.clock=lambda:stamp(9,9)
        self.assertFalse(self.w._scan_due({'last_daily_scan':'2026-11-08'}))
        self.assertTrue(self.w._scan_due({'last_daily_scan':'2026-11-07'}))

class HourlyScopeTests(unittest.TestCase):
    def test_hourly_scan_updates_cloud_but_only_queues_new_unsuppressed_episode(self):
        f=fixtures.WorkerExperienceTests('runTest');f.setUp();self.addCleanup(f.doCleanups)
        w=f.worker;w.config['scan_interval_seconds']=3600;clock=[stamp(9,19)];w.clock=lambda:clock[0]
        f.provider.rows=[fixtures.row(1),fixtures.row(100)]
        with w.store.locked():
            sub=f.active();w._process_show(f.show,sub,False)
            self.assertFalse(w._scan_due(sub))
            original=copy.deepcopy(sub['activation']);before=set(w.store.state['downloads']);key=next(iter(w.store.state['downloads'].values()))['logical_key']
            sub['local_suppressions']={key:{'reason':'published_local_file_missing','scope':'all_versions_of_this_logical_episode'}}
            for j in w.store.state['downloads'].values():j['status']='cancelled'
            f.provider.rows += [fixtures.row(99),fixtures.row(100,'8K'),fixtures.row(101)]
            f.probes[f.provider.rows[-2]['name']]=fixtures.probe(7680,4320)
            clock[0]=stamp(9,20);self.assertTrue(w._scan_due(sub));w._process_show(f.show,sub,False)
            added=[j for k,j in w.store.state['downloads'].items() if k not in before]
            self.assertEqual([j['candidate']['episode'] for j in added],[101])
            self.assertEqual(sub['activation'],original)
            self.assertIn(key,sub['local_suppressions'])
            cloud_episodes={x['selected']['episode'] for x in f.cloud.selections}
            self.assertTrue({100,101}.issubset(cloud_episodes))
            cloud_names={x['name'] for x in w.store.state['inventory']['cloud_inventory:test']['files']}
            self.assertTrue({'1_4K.mkv','99_4K.mkv'}.issubset(cloud_names))
            self.assertFalse(w._scan_due(sub))
            w._process_show(f.show,sub,False)
            self.assertEqual(len(w.store.state['downloads']),len(before)+1)
