import copy
import importlib.util
from pathlib import Path,PurePosixPath
import tempfile
import unittest
from unittest.mock import Mock
from home_media_follow.store import Store

from home_media_follow import quarantine as repair


class Provider:
    def __init__(self,row):
        self.rows={row['path']:dict(row)}
    def list_target(self,path):
        return [copy.deepcopy(r) for r in self.rows.values() if str(PurePosixPath(r['path']).parent)==path]


class OpenList:
    def __init__(self,provider,lost=False): self.provider,self.lost,self.moves=provider,lost,0
    def list(self,path,refresh=False): return self.provider.list_target(path.removeprefix('/Baidu'))
    def fs(self,operation,body):
        if operation=='mkdir':
            p=body['path'].removeprefix('/Baidu');self.provider.rows[p]={'id':'dir-'+p,'path':p,'name':PurePosixPath(p).name,'size':0,'is_dir':True}
        elif operation=='move':
            self.moves+=1
            for name in body['names']:
                src=body['src_dir'].removeprefix('/Baidu')+'/'+name;dst=body['dst_dir'].removeprefix('/Baidu')+'/'+name
                row=self.provider.rows.pop(src);row['path']=dst;self.provider.rows[dst]=row
            if self.lost:
                self.lost=False;raise OSError('simulated lost response')
        else: raise AssertionError('Deletion is never permitted')


class CandidateRepairTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=Store(Path(self.temp.name).resolve()/'state');self.shows={'demo':{'show_id':'demo','cloud_path':'/HomeMediaFollow/Demo'}}
        self.row={'id':'901','path':'/HomeMediaFollow/Demo/01-1080.mp4','name':'01-1080.mp4','size':4,'provider_fingerprint':'opaque-a','is_dir':False}
        self.candidate={'record_type':'candidate','show_id':'demo','season':1,'episode':1,'candidate_id':'c'*64,'source_id':'101','size':4,'name':self.row['name'],'managed_cloud':True,'cloud':copy.deepcopy(self.row),'provider_fingerprint':'opaque-a','transfer_operation_id':'transfer'}
        with self.store.locked():
            self.store.state['inventory']['c'*64]=self.candidate
            self.store.state['operations']['transfer']={'kind':'cloud_transfer','status':'confirmed','show_id':'demo','candidate_id':'c'*64,'before_ids':[], 'files':[{'id':'101','size':4,'provider_fingerprint':'opaque-a'}]}
            self.store.save()

    def test_only_unselected_exact_owned_copy_moves_without_delete(self):
        provider=Provider(self.row);openlist=OpenList(provider)
        with self.store.locked(): result=repair.apply_plan(self.store,provider,openlist,self.shows)
        self.assertEqual(result['status'],'complete');self.assertEqual(result['deletes'],0);self.assertEqual(openlist.moves,1)
        self.assertEqual(len([r for r in provider.rows.values() if r['id']=='901']),1)
        self.assertIn('/_follow_candidates/',self.store.state['inventory']['c'*64]['cloud']['path'])

    def test_import_or_current_or_inflight_reference_is_untouched(self):
        with self.store.locked():
            self.store.state['inventory']['selection:demo']={'record_type':'cloud_selection','cloud':copy.deepcopy(self.row)}
            self.assertEqual(repair.repair_plan(self.store.state,self.shows),[])
            self.store.state['inventory'].pop('selection:demo')
            self.store.state['downloads']['busy']={'status':'downloading','candidate':{'cloud':copy.deepcopy(self.row)}}
            self.assertEqual(repair.repair_plan(self.store.state,self.shows),[])
            self.store.state['downloads'].clear();self.store.state['inventory']['c'*64].pop('transfer_operation_id')
            self.assertEqual(repair.repair_plan(self.store.state,self.shows),[])

    def test_lost_move_response_recovers_by_exact_id_without_second_move(self):
        provider=Provider(self.row);openlist=OpenList(provider,lost=True)
        with self.store.locked():
            self.assertEqual(repair.apply_plan(self.store,provider,openlist,self.shows)['status'],'pending')
        recovered=Store(self.store.root)
        with recovered.locked(): result=repair.apply_plan(recovered,provider,openlist,self.shows)
        self.assertEqual(result['status'],'complete');self.assertEqual(openlist.moves,1)

    def test_changed_native_identity_cannot_move_another_file(self):
        provider=Provider({**self.row,'id':'999'});openlist=OpenList(provider)
        with self.store.locked(): result=repair.apply_plan(self.store,provider,openlist,self.shows)
        self.assertEqual(result['status'],'pending');self.assertEqual(openlist.moves,0)

    def test_preexisting_target_id_never_becomes_owned_repair(self):
        with self.store.locked():
            self.store.state['operations']['transfer']['before_ids']=['901']
            self.assertEqual(repair.repair_plan(self.store.state,self.shows),[])

    def retirement(self):
        op_id='cloud-upgrade'
        return {'kind':'cloud_replacement','operation_id':op_id,'phase':'confirmed',
            'show_id':'demo','root':self.shows['demo']['cloud_path'],
            'old':{**self.row,'path':'/HomeMediaFollow/Demo/_follow_recovery/'+op_id+'/'+self.row['name']},
            'new':{**self.row,'id':'902','name':'01-4K.mp4','path':'/HomeMediaFollow/Demo/01-4K.mp4'}}

    def pending_retired(self):
        operation=repair.repair_plan(self.store.state,self.shows)[0]
        operation.update(phase='prepared',last_error={'type':'ValueError'})
        self.store.state['operations'][operation['operation_id']]=operation
        self.store.state['operations']['cloud-upgrade']=self.retirement()
        self.store.event('fixture_pending_retired',{})
        return operation

    def test_precise_confirmed_retirement_is_excluded_from_new_plans(self):
        with self.store.locked():
            self.store.state['operations']['cloud-upgrade']=self.retirement()
            self.assertEqual(repair.repair_plan(self.store.state,self.shows),[])

    def test_retirement_requires_exact_identity_and_registered_recovery_scope(self):
        changes=[('phase','retire_intent'),('show_id','other'),('root','/HomeMediaFollow/Other'),
            ('operation_id','other'),('old.id','999'),('old.size',5),('old.provider_fingerprint',None),
            ('old.provider_fingerprint','different'),('old.path','/HomeMediaFollow/Demo/_follow_recovery/other/'+self.row['name']),
            ('old.path','/HomeMediaFollow/Demo/_follow_recovery/cloud-upgrade/another.mp4')]
        with self.store.locked():
            for field,value in changes:
                with self.subTest(field=field,value=value):
                    operation=self.retirement()
                    if field.startswith('old.'): operation['old'][field[4:]]=value
                    else: operation[field]=value
                    self.store.state['operations']['cloud-upgrade']=operation
                    self.assertIsNone(repair.confirmed_retirement(self.store.state,self.shows['demo'],self.row))
                    # A live replacement also protects its old ID from all maintenance.
                    self.assertEqual(len(repair.repair_plan(self.store.state,self.shows)),0 if field=='phase' else 1)

    def test_pending_retired_finishes_read_only_and_preserves_history_across_restart(self):
        provider=Provider({**self.row,'id':'different-same-name'})
        provider.list_target=Mock(wraps=provider.list_target)
        openlist=OpenList(provider);openlist.fs=Mock(side_effect=AssertionError('Read-only retirement reconciliation'))
        with self.store.locked():
            pending=self.pending_retired();op_id=pending['operation_id']
            retirement_before=copy.deepcopy(self.store.state['operations']['cloud-upgrade'])
        recovered=Store(self.store.root)
        with recovered.locked():
            result=repair.apply_plan(recovered,provider,openlist,self.shows)
            self.assertEqual(result['status'],'complete')
            self.assertEqual(result['files'][0]['status'],'already_retired')
            self.assertEqual(result['deletes'],0)
            operation=recovered.state['operations'][op_id]
            self.assertEqual((operation['phase'],operation['outcome']),('confirmed','already_retired'))
            self.assertEqual(operation['resolved_error'],{'type':'ValueError'})
            candidate=recovered.state['inventory']['c'*64]
            self.assertEqual(candidate['cloud'],self.row)
            self.assertEqual(candidate['retired_by']['operation_id'],'cloud-upgrade')
            self.assertEqual(recovered.state['operations']['cloud-upgrade'],retirement_before)
            self.assertEqual(provider.list_target.call_count,2)
            self.assertIn(pending['source']['path'],provider.rows)
            self.assertEqual(repair.apply_plan(recovered,provider,openlist,self.shows)['files'],[])
            self.assertEqual(provider.list_target.call_count,2)
        openlist.fs.assert_not_called()
        final=Store(self.store.root)
        with final.locked(): self.assertEqual(final.state['operations'][op_id]['outcome'],'already_retired')
        self.assertIn('candidate_quarantine_already_retired',final.journal_path.read_text(encoding="utf-8"))

    def test_pending_retirement_never_confirms_when_native_id_still_exists_or_changed(self):
        with self.store.locked():
            pending=self.pending_retired()
            for path in (self.row['path'],'/HomeMediaFollow/Demo/renamed.mp4',pending['destination']):
                for changes in ({},{'size':99},{'provider_fingerprint':'changed'}):
                    with self.subTest(path=path,changes=changes):
                        provider=Provider({**self.row,**changes,'path':path})
                        openlist=OpenList(provider);openlist.fs=Mock(side_effect=AssertionError('No mutation'))
                        result=repair.apply_plan(self.store,provider,openlist,self.shows)
                        self.assertEqual(result['status'],'pending')
                        self.assertEqual(pending['phase'],'prepared')
                        self.assertNotIn('retired_by',self.store.state['inventory']['c'*64])
                        openlist.fs.assert_not_called()

    def test_pending_retirement_failed_read_stays_pending_without_mutation(self):
        with self.store.locked():
            pending=self.pending_retired()
            provider=Provider(self.row);provider.list_target=Mock(side_effect=[[],OSError('unavailable')])
            openlist=OpenList(provider);openlist.fs=Mock(side_effect=AssertionError('No mutation'))
            result=repair.apply_plan(self.store,provider,openlist,self.shows)
            self.assertEqual(result['status'],'pending')
            self.assertEqual(pending['phase'],'prepared')
            self.assertNotIn('retired_by',self.store.state['inventory']['c'*64])
            openlist.fs.assert_not_called()
