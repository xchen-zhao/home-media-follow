"""One-time, restartable repair of owned unselected candidates in show roots.

Files are moved, never deleted. Initial library imports are not eligible.
"""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import sys
import time
from home_media_follow.baidu import BaiduAdapter
from home_media_follow.cloud import CloudVersions
from home_media_follow.integrations import OpenList
from home_media_follow.runtime import atomic_json
from home_media_follow.secrets import SecretStore
from home_media_follow.store import Store


def referenced_ids(state):
    result=set()
    def add(row):
        if isinstance(row,dict) and row.get('id'): result.add(str(row['id']))
    for value in state['inventory'].values():
        if value.get('record_type')=='cloud_selection': add(value.get('cloud'))
    for sub in state['subscriptions'].values():
        for asset in sub.get('assets',{}).values(): add(asset.get('candidate',{}).get('cloud'))
    for job in state['downloads'].values():
        if job.get('status') not in {'complete','cancelled','rejected'}:
            add(job.get('candidate',{}).get('cloud'))
            operation=state['operations'].get(job.get('cloud_operation_id'),{})
            add(operation.get('old'));add(operation.get('new'))
    for operation in state['operations'].values():
        if operation.get('kind')=='cloud_replacement' and operation.get('phase')!='confirmed':
            add(operation.get('old'));add(operation.get('new'))
    return result


def confirmed_retirement(state, show, wanted):
    """Find a precise completed retirement, preserving its moved recovery path."""
    fingerprint=wanted.get('provider_fingerprint')
    if not fingerprint or str(PurePosixPath(wanted.get('path','')).parent)!=show['cloud_path']:
        return None
    for operation_id,operation in sorted(state['operations'].items()):
        old=operation.get('old')
        if (operation.get('kind')!='cloud_replacement' or operation.get('phase')!='confirmed'
                or operation.get('show_id')!=show['show_id'] or operation.get('root')!=show['cloud_path']
                or operation.get('operation_id')!=operation_id or not isinstance(old,dict)):
            continue
        if (str(old.get('id'))==str(wanted.get('id')) and old.get('size')==wanted.get('size')
                and old.get('provider_fingerprint')==fingerprint
                and str(PurePosixPath(old.get('path','')).parent)==show['cloud_path']+'/_follow_recovery/'+operation_id
                and PurePosixPath(old['path']).name==PurePosixPath(wanted['path']).name):
            return operation_id,operation
    return None


def repair_plan(state, shows):
    active=referenced_ids(state);result=[]
    for cid,candidate in state['inventory'].items():
        if candidate.get('record_type')!='candidate' or not candidate.get('managed_cloud'): continue
        show=shows.get(candidate.get('show_id'));cloud=candidate.get('cloud',{})
        transfer=state['operations'].get(candidate.get('transfer_operation_id'),{})
        if not show or not cloud.get('id') or str(cloud['id']) in active: continue
        if str(PurePosixPath(cloud.get('path','')).parent)!=show['cloud_path']: continue
        if (transfer.get('kind')!='cloud_transfer' or transfer.get('status')!='confirmed'
                or transfer.get('candidate_id')!=cid or transfer.get('show_id')!=show['show_id']): continue
        source=next((row for row in transfer.get('files',[]) if str(row.get('id'))==candidate['source_id'] and row.get('size')==candidate['size']),None)
        fingerprint=candidate.get('provider_fingerprint')
        if (not source or not fingerprint or source.get('provider_fingerprint')!=fingerprint
                or cloud.get('provider_fingerprint')!=fingerprint or str(cloud['id']) in set(map(str,transfer.get('before_ids',[])))): continue
        if confirmed_retirement(state,show,cloud): continue
        destination=show['cloud_path']+'/_follow_candidates/'+cid[:24]+'/'+PurePosixPath(cloud['path']).name
        op_id='candidate-quarantine-'+hashlib.sha256((cid+':'+str(cloud['id'])).encode()).hexdigest()[:24]
        prior=state['operations'].get(op_id)
        if prior and prior.get('phase')=='confirmed': continue
        result.append(prior or {'kind':'candidate_quarantine','operation_id':op_id,'show_id':show['show_id'],
            'candidate_id':cid,'source':dict(cloud),'destination':destination,'phase':'prepared'})
    # Include interrupted moves even if the candidate metadata was updated later.
    known={op['operation_id'] for op in result}
    for op in state['operations'].values():
        if op.get('kind')=='candidate_quarantine' and op.get('phase')!='confirmed' and op['operation_id'] not in known:
            result.append(op)
    return result


def apply_plan(store, provider, openlist, shows, *, dry_run=False):
    plans=repair_plan(store.state,shows);results=[]
    if dry_run: return {'status':'planned','files':plans,'deletes':0}
    versions=CloudVersions(store,provider,openlist,mount=(openlist.mount if isinstance(getattr(openlist,"mount",None),str) else "/Baidu"))
    def find(path):
        return next((r for r in provider.list_target(str(PurePosixPath(path).parent)) if r['path']==path),None)
    def matches(row,wanted):
        return bool(row and row['id']==wanted['id'] and row['size']==wanted['size']
            and row.get('provider_fingerprint')==wanted.get('provider_fingerprint'))
    for operation in plans:
        op_id=operation['operation_id'];wanted=operation['source'];cid=operation['candidate_id']
        try:
            if str(wanted['id']) in referenced_ids(store.state): raise ValueError('Candidate is currently referenced')
            candidate=store.state['inventory'][cid];show=shows[operation['show_id']]
            if (candidate.get('transfer_operation_id') not in store.state['operations']
                    or str(PurePosixPath(wanted['path']).parent)!=show['cloud_path']
                    or not operation['destination'].startswith(show['cloud_path']+'/_follow_candidates/')):
                raise ValueError('Quarantine scope changed')
            store.state['operations'][op_id]=operation
            retired=confirmed_retirement(store.state,show,wanted)
            if retired:
                current=candidate.get('cloud',{})
                if not matches(current,wanted): raise ValueError('Retired candidate pointer identity changed')
                parent=str(PurePosixPath(operation['destination']).parent)
                root_rows=provider.list_target(show['cloud_path'])
                target_rows=provider.list_target(parent)
                # The same native ID anywhere in either directory remains an
                # unresolved identity conflict, including renamed/changed rows.
                if any(str(row.get('id'))==str(wanted['id']) for row in root_rows+target_rows):
                    raise ValueError('Retired cloud identity still present')
                retired_id,retirement=retired
                proof={'operation_id':retired_id,'id':wanted['id'],'size':wanted['size'],
                    'provider_fingerprint':wanted['provider_fingerprint'],'original_path':wanted['path'],
                    'recovery_path':retirement['old']['path'],'checked_directories':[show['cloud_path'],parent],
                    'checked_at':time.time()}
                candidate['retired_by']=dict(proof)
                operation.update(phase='confirmed',outcome='already_retired',retirement=dict(proof))
                if 'last_error' in operation: operation['resolved_error']=operation.pop('last_error')
                store.event('candidate_quarantine_already_retired',{'operation_id':op_id,
                    'retirement_operation_id':retired_id,'candidate_id':cid,'id':wanted['id']})
                results.append({'candidate_id':cid,'id':wanted['id'],'source':wanted['path'],
                    'destination':operation['destination'],'status':'already_retired','retirement_operation_id':retired_id})
                continue
            store.event('candidate_quarantine_prepared',{'operation_id':op_id})
            parent=str(PurePosixPath(operation['destination']).parent)
            versions.mkdir(show['cloud_path']+'/_follow_candidates');versions.mkdir(parent)
            old=find(wanted['path']);new=find(operation['destination'])
            if not matches(new,wanted):
                if new or not matches(old,wanted): raise ValueError('Exact cloud candidate identity changed')
                if operation['phase']=='move_intent': raise ValueError('Previous move requires reconciliation')
                operation['phase']='move_intent';store.event('candidate_quarantine_move_intent',{'operation_id':op_id})
                openlist.fs('move',{'src_dir':(openlist.mount if isinstance(getattr(openlist,'mount',None),str) else '/Baidu')+show['cloud_path'],'dst_dir':(openlist.mount if isinstance(getattr(openlist,'mount',None),str) else '/Baidu')+parent,'names':[PurePosixPath(wanted['path']).name]})
                new=find(operation['destination'])
                if not matches(new,wanted): raise ValueError('Move outcome requires reconciliation')
            old=find(wanted['path'])
            if matches(old,wanted): raise ValueError('Candidate still appears at both paths')
            candidate['cloud']=new;candidate['quarantined_at']=time.time()
            operation['phase']='confirmed';operation.pop('last_error',None)
            store.event('candidate_quarantine_confirmed',{'operation_id':op_id})
            results.append({'candidate_id':cid,'id':wanted['id'],'source':wanted['path'],'destination':new['path'],'status':'quarantined'})
        except Exception as error:
            operation['last_error']={'type':type(error).__name__};store.state['operations'][op_id]=operation
            store.event('candidate_quarantine_pending',{'operation_id':op_id,'error_type':type(error).__name__})
            results.append({'candidate_id':cid,'id':wanted['id'],'source':wanted['path'],'destination':operation['destination'],'status':'pending','error_type':type(error).__name__})
            if getattr(error,'code',None)==429:
                store.state['operations']['provider-backoff']={'kind':'provider_backoff','until':time.time()+max(1800,getattr(error,'retry_after',0) or 0)}
                store.save();break
    return {'status':'complete' if len(results)==len(plans) and all(r['status'] in {'quarantined','already_retired'} for r in results) else 'pending','files':results,'deletes':0}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--root',default='C:/HomeMediaFollow');parser.add_argument('--dry-run',action='store_true');args=parser.parse_args()
    root=Path(args.root);config=json.loads((root/'config/shows.json').read_bytes());shows={s['show_id']:s for s in config['shows']}
    store=Store(root/'state');secrets=SecretStore(root/'secrets/service.dpapi')
    with store.locked():
        if time.time()<store.state['operations'].get('provider-backoff',{}).get('until',0):
            result={'status':'deferred_provider_backoff','files':[],'deletes':0}
        else:
            result=apply_plan(store,BaiduAdapter(secrets.baidu_session,cloud_root=config['cloud_root']),OpenList(secrets.load()['openlist'],base_url=config['services']['openlist'],mount=config['openlist_mount'],cloud_root=config['cloud_root']),shows,dry_run=args.dry_run)
        atomic_json(root/'reports/candidate-quarantine.json',result)
    print(json.dumps({'status':result['status'],'files':len(result['files']),'deletes':0}))
    return 0 if result['status'] in {'complete','planned'} else 1


if __name__=='__main__': raise SystemExit(main())
