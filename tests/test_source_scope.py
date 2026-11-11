import copy,unittest
from home_media_follow.core import parse_source_episode,candidate_in_source_scope,activation,in_requested_range
import test_worker_reliability as fixtures
class SourceSeasonScopeTests(unittest.TestCase):
 def setUp(self):
  self.show={'show_id':'variety','media_kind':'variety','season':4,'episode_mode':'absolute','source_scope':{'season':4,'allowed_parent_paths':['.','第四季（示例高码率）']}}
 def row(self,name='2025.02.7-第5期上-4K.mp4',parent=''):
  return {'name':name,'path':'/resource/'+(parent+'/' if parent else '')+name}
 def test_root_and_registered_folder_belong_to_season4(self):
  for parent in ['', '第四季（示例高码率）']:
   self.assertEqual(parse_source_episode(self.show,self.row(parent=parent),'/resource'),{'season':4,'episode':5,'part':'上'})
 def test_old_season_ten_cannot_be_latest(self):
  rows=[self.row(),self.row(name='2025.02.8-第5期下.mp4'),self.row(name='2024.12.27-第10期上.mkv',parent='历史季/第三季')]
  candidates=[{'show_id':'variety',**p} for r in rows if (p:=parse_source_episode(self.show,r,'/resource'))]
  active=activation(candidates,'scoped');self.assertEqual(active['watermark'],{'season':4,'episode':5});self.assertEqual(len(active['initial_selected']),2)
  self.assertFalse(in_requested_range({'show_id':'variety','season':4,'episode':4,'part':'下'},active))
  self.assertTrue(in_requested_range({'show_id':'variety','season':4,'episode':6,'part':'上'},active))
 def test_unknown_folder_and_outside_root_rejected(self):
  for root in ['/other','/res']:
   self.assertIsNone(parse_source_episode(self.show,self.row(),root))
  self.assertIsNone(parse_source_episode(self.show,self.row(parent='另一个目录'),'/resource'))
 def test_explicit_conflicting_season_rejected(self):
  self.assertIsNone(parse_source_episode(self.show,self.row(name='S03E05-上.mp4'),'/resource'))
 def test_bad_scope_does_not_fall_back(self):
  self.show['source_scope']['allowed_parent_paths']=['../old']
  with self.assertRaises(ValueError):parse_source_episode(self.show,self.row(),'/resource')
 def test_legacy_anime_unchanged(self):
  show={'media_kind':'anime','season':1,'episode_mode':'absolute'}
  self.assertEqual(parse_source_episode(show,{'name':'199-4K.mp4','path':'/anything'},'/resource')['episode'],199)
 def test_cached_old_identity_rejected_even_at_correct_path(self):
  c={'name':self.row()['name'],'source_path':self.row()['path'],'season':1,'episode':5,'part':'上'}
  self.assertFalse(candidate_in_source_scope(self.show,c,'/resource'));c['season']=4;self.assertTrue(candidate_in_source_scope(self.show,c,'/resource'))
 def test_queue_guard_blocks_replayed_old_season(self):
  fixture=fixtures.WorkerReliabilityTests('runTest');fixture.setUp();self.addCleanup(fixture.doCleanups);w=fixture.worker
  show={**self.show,'show_id':'show','title':'Fixture','cloud_path':'/HomeMediaFollow/Fixture','source':{'source_root':'/resource'}}
  c=copy.deepcopy(fixture.candidate);c.update(source_path='/resource/历史季/第三季/第10期上.mp4',name='第10期上.mp4',episode=10,part='上')
  with w.store.locked():w._queue(show,w.store.state['subscriptions']['show'],c,None)
  self.assertFalse(w.aria2.adds);self.assertFalse(w.store.state['downloads'])

 def test_variety_metadata_preserves_routed_date_period_and_part(self):
  from unittest.mock import Mock,patch
  from home_media_follow.worker import Worker
  w=Worker.__new__(Worker);w.publisher=Mock();w.publisher.return_value.route.return_value=('relative.mp4','2025.02.8 第5期下')
  with patch('home_media_follow.worker.Path.read_text',return_value='{}'):
   label=w._publication_label({**self.show,'cloud_path':'/HomeMediaFollow/ExampleVariety'},{'episode':5,'name':'2025.02.8-第5期下-4K.mp4'})
  self.assertEqual(label,'2025.02.8 第5期下')
 def test_anime_metadata_label_unchanged(self):
  from home_media_follow.worker import Worker
  w=Worker.__new__(Worker)
  self.assertEqual(w._publication_label({'media_kind':'anime'},{'episode':199}),'第199集')
