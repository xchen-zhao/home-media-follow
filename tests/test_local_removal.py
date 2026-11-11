"""Deleting published media opts out of local refill, including quality upgrades."""
import copy
import json
from pathlib import Path
import shutil
import unittest
import uuid

from home_media_follow.core import activation, candidate_identity, logical_key
from home_media_follow.local_policy import local_requested
from home_media_follow.store import Store
from home_media_follow.work_due import source_work_due
import test_worker_reliability as fixtures


class LocalRemovalTests(unittest.TestCase):
    def setUp(self):
        fixture=fixtures.WorkerReliabilityTests('runTest');fixture.setUp();self.addCleanup(fixture.doCleanups)
        self.w=fixture.worker;self.root=fixture.root;self.show=fixture.show
        rpc=self.w.aria2.call
        def call(method,params=None):
            if method=='tellStatus':return copy.deepcopy(self.w.aria2.tasks[params[0]])
            return rpc(method,params)
        self.w.aria2.call=call
        self.lock=self.w.store.locked();self.lock.__enter__();self.addCleanup(self.lock.__exit__,None,None,None)
        self.c=copy.deepcopy(fixture.candidate);self.c['record_type']='candidate'
        self.c['probe'].update(width=1920,height=1080)
        self.c['candidate_id']=candidate_identity(self.c);self.key=logical_key(self.c)
        media=Path(self.w.config['media_root']);media.mkdir();self.path=media/'E100.mp4'
        self.path.write_bytes(b'x'*100)
        self.sub=self.w.store.state['subscriptions']['show']
        self.sub.update(activation=activation([self.c],'initial'),scan_candidates=[self.c['candidate_id']])
        self.sub['assets'][self.key]={'candidate':copy.deepcopy(self.c),'destination':str(self.path),
            'sha256':'trusted-original','managed_local':True}
        self.w.store.state['inventory'][self.c['candidate_id']]=self.c
        self.w.store.state['downloads']['original']={'download_id':'original','gid':'1111111111111111',
            'show_id':'show','logical_key':self.key,'candidate':copy.deepcopy(self.c),
            'destination':str(self.path),'status':'complete','jellyfin':{'status':'verified','playable':True}}
        self.w._destination=lambda show,c,current=None:Path(current['destination']) if current else media/f"E{c['episode']}.mp4"
        self.w.store.save()

    def newer(self,episode=100,width=3840):
        c=copy.deepcopy(self.c);c.update(episode=episode,source_id=f'new-{episode}-{width}',
            name=f'{episode}-{width}.mp4',source_path=f'/source/{episode}-{width}.mp4')
        c['probe'].update(width=width,height=width*9//16)
        c['cloud']=dict(c['cloud'],id=f'new-{episode}-{width}',path=f"/HomeMediaFollow/Fixture/{c['name']}")
        c['candidate_id']=candidate_identity(c);return c

    def test_deleted_completed_episode_never_queues_higher_quality(self):
        self.path.unlink()
        with self.w.store.locked():self.w._queue(self.show,self.sub,self.newer(),None)
        self.assertIn(self.key,self.sub['local_suppressions'])
        self.assertEqual(len(self.w.store.state['downloads']),1)
        self.assertFalse(self.w.aria2.adds)

    def test_marker_survives_restart_and_covers_changed_candidate_id(self):
        self.path.unlink()
        with self.w.store.locked():self.w.observe_local_removals()
        # The lock is a live synchronization artifact, never persisted state.
        copied=self.root/'restarted-state'
        shutil.copytree(self.root/'state',copied,ignore=shutil.ignore_patterns(self.w.store.lock_path.name))
        restored=Store(copied).state['subscriptions']['show']
        self.assertFalse(local_requested(self.newer(width=7680),restored))
        self.assertTrue(local_requested(self.newer(101),restored))

    def test_next_episode_still_queues_after_old_episode_deleted(self):
        self.path.unlink()
        with self.w.store.locked():
            self.w.observe_local_removals();self.w._queue(self.show,self.sub,self.newer(101),None)
        self.assertEqual(len(self.w.aria2.adds),1)
        jobs=[j for j in self.w.store.state['downloads'].values() if j['status']=='queued']
        self.assertEqual([j['candidate']['episode'] for j in jobs],[101])

    def test_existing_watched_or_unwatched_file_is_not_deleted_or_suppressed(self):
        with self.w.store.locked():self.w.observe_local_removals()
        self.assertTrue(self.path.is_file())
        self.assertFalse(self.sub.get('local_suppressions'))

    def test_unavailable_media_root_does_not_persist_a_user_removal(self):
        self.path.unlink();self.w.config['media_root']=str(self.root/'offline')
        with self.w.store.locked():self.w._queue(self.show,self.sub,self.newer(),None)
        self.assertFalse(self.sub.get('local_suppressions'))
        self.assertFalse(self.w.aria2.adds)

    def test_incomplete_publication_cannot_be_misclassified_as_manual_deletion(self):
        self.path.unlink()
        self.w.store.state['downloads']['original']['status']='published'
        with self.w.store.locked():self.w.observe_local_removals()
        self.assertFalse(self.sub.get('local_suppressions'))

    def test_recoverable_backup_transaction_does_not_create_permanent_opt_out(self):
        self.path.unlink();job={'download_id':'upgrade','show_id':'show','logical_key':self.key,'status':'downloading'}
        self.w.store.state['downloads']['upgrade']=job
        self.w.store.state['operations']['local-upgrade']={'kind':'replacement','phase':'backed_up'}
        with self.w.store.locked():self.w.observe_local_removals()
        self.assertFalse(self.sub.get('local_suppressions'))

    def test_resume_and_reactivation_do_not_erase_episode_opt_outs(self):
        self.path.unlink()
        with self.w.store.locked():self.w.observe_local_removals()
        for action in ['resume','activate_latest']:
            request={'request_id':str(uuid.uuid4()),'show_ids':['show'],'action':action,
                     'created_at':'2026-11-08T01:00:00+00:00','policy_revision':3}
            (self.root/'requests'/f"{request['request_id']}.json").write_text(json.dumps(request))
            with self.w.store.locked():self.w.inbox()
            self.assertIn(self.key,self.sub['local_suppressions'])

    def test_daily_scan_keeps_cloud_selection_without_local_refill(self):
        self.path.unlink();new=self.newer();new['managed_cloud']=True
        self.w._source=lambda show,sub:({},[new])
        self.w._probe_candidate=lambda *args:None
        self.w.provider.list_target.return_value=[dict(new['cloud'],name=new['name'],size=100)]
        self.w.cloud.select.return_value='cloud-new'
        with self.w.store.locked():self.w._process_show(self.show,self.sub,True)
        self.assertIn('selection:'+self.key,self.w.store.state['inventory'])
        self.w.cloud.confirm.assert_called_once_with('cloud-new')
        self.assertFalse(self.w.aria2.adds)
        self.assertEqual(len(self.w.store.state['downloads']),1)

    def test_recovery_due_ignores_deleted_episode_but_detects_new_episode(self):
        self.path.unlink();new=self.newer()
        with self.w.store.locked():self.w.observe_local_removals()
        self.w.store.state['inventory'][new['candidate_id']]=new
        self.sub['scan_candidates']=[new['candidate_id']]
        self.assertFalse(source_work_due(self.w.store.state,self.sub,2000))
        next_ep=self.newer(101);self.w.store.state['inventory'][next_ep['candidate_id']]=next_ep
        self.sub['scan_candidates'].append(next_ep['candidate_id'])
        self.assertTrue(source_work_due(self.w.store.state,self.sub,2000))

    def test_already_queued_same_episode_upgrade_is_cancelled_without_touching_next_episode(self):
        with self.w.store.locked():
            self.w._queue(self.show,self.sub,self.newer(),None)
            self.w._queue(self.show,self.sub,self.newer(101),None)
        old_job=next(j for j in self.w.store.state['downloads'].values() if j['status']=='queued' and j['logical_key']==self.key)
        next_job=next(j for j in self.w.store.state['downloads'].values() if j['status']=='queued' and j['logical_key']!=self.key)
        for job in [old_job,next_job]:self.w.aria2.tasks[job['gid']]['files']=[{'path':job['stage_file']}]
        self.path.unlink()
        with self.w.store.locked():
            self.w.observe_local_removals();self.w.reconcile_suppressed_downloads();self.w.completions()
        self.assertEqual(old_job['status'],'cancelled')
        self.assertIn(('remove',[old_job['gid']]),self.w.aria2.controls)
        self.assertNotEqual(next_job['status'],'cancelled')
        self.assertEqual(self.w.aria2.tasks[next_job['gid']]['status'],'active')

    def test_reappearance_of_file_does_not_implicitly_restore_download_permission(self):
        self.path.unlink()
        with self.w.store.locked():self.w.observe_local_removals()
        self.path.write_bytes(b'x'*100)
        with self.w.store.locked():self.w._queue(self.show,self.sub,self.newer(),None)
        self.assertFalse(self.w.aria2.adds)

    def test_gid_rebound_to_other_path_is_not_cancelled_or_published(self):
        with self.w.store.locked():self.w._queue(self.show,self.sub,self.newer(),None)
        job=next(j for j in self.w.store.state['downloads'].values() if j['status']=='queued')
        self.w.aria2.tasks[job['gid']]['files']=[{'path':str(self.root/'unrelated.mp4')}]
        self.path.unlink()
        with self.w.store.locked():
            self.w.observe_local_removals();self.w.reconcile_suppressed_downloads();self.w.completions()
        self.assertEqual(job['status'],'cancel_pending')
        self.assertNotIn(('remove',[job['gid']]),self.w.aria2.controls)
        self.assertFalse(self.path.exists())

    def test_tampered_manifest_blocks_cancel_and_publication(self):
        with self.w.store.locked():self.w._queue(self.show,self.sub,self.newer(),None)
        job=next(j for j in self.w.store.state['downloads'].values() if j['status']=='queued')
        (self.root/'state/manifests'/f"{job['gid']}.json").write_text('{}')
        self.path.unlink()
        with self.w.store.locked():
            self.w.observe_local_removals();self.w.reconcile_suppressed_downloads();self.w.completions()
        self.assertEqual(job['status'],'cancel_pending')
        self.assertFalse(self.w.aria2.controls)
        self.assertFalse(self.path.exists())

    def test_cancelled_local_upgrade_still_finishes_cloud_only_confirmation(self):
        with self.w.store.locked():self.w._queue(self.show,self.sub,self.newer(),'cloud-upgrade')
        job=next(j for j in self.w.store.state['downloads'].values() if j['status']=='queued')
        self.w.aria2.tasks[job['gid']]['files']=[{'path':job['stage_file']}]
        self.w.store.state['operations']['cloud-upgrade']={'kind':'cloud_replacement','phase':'switched'}
        attempts=[]
        def confirm(operation):
            attempts.append(operation)
            if len(attempts)==1:raise OSError('Cloud temporarily unavailable')
            self.w.store.state['operations'][operation]['phase']='confirmed'
        self.w.cloud.confirm.side_effect=confirm
        self.path.unlink()
        with self.w.store.locked():
            self.w.observe_local_removals();self.w.reconcile_suppressed_downloads()
            self.assertEqual(job['status'],'cancelled')
            self.assertTrue(job['cloud_confirmation_pending'])
            self.w.reconcile_suppressed_downloads()
        self.assertNotIn('cloud_confirmation_pending',job)
        self.assertEqual(self.w.store.state['operations']['cloud-upgrade']['phase'],'confirmed')
        self.assertEqual(len(self.w.aria2.adds),1)
