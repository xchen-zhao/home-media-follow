"""Windows-hosted subscriptions. Inventory, requests and side effects are separate."""
from __future__ import annotations
import argparse
from collections import defaultdict
import copy
from datetime import datetime, timezone, timedelta
import hashlib
import importlib.util
import json
from pathlib import Path, PurePosixPath
import re
import ntpath
import shutil
import sys
import time

from .baidu import BaiduAdapter, TransferUnknown, BaiduError
from .cloud import CloudVersions, CloudPending
from .catalog import Catalog
from .core import (activation, candidate_identity, choose_candidate, in_requested_range,
                   parse_source_episode, candidate_in_source_scope,
                   logical_key, needs_upgrade, parse_episode, resolution_rank)
from .gateway import validate_request, ordered_inbox
from .hook import gate
from .integrations import OpenList, Aria2, Jellyfin
from .probe import probe_url, probe_file
from .replacement import Replacement, file_sha256
from .runtime import atomic_json, publisher_lock, read_manifest, write_manifest
from .secrets import SecretStore
from .store import Store
from .local_policy import completed_asset_receipt, local_requested
from .work_due import candidate_quarantine_holds, pending_cloud_keys, pending_cloud_transfers, source_work_due

VIDEO = {'.mp4','.mkv','.m4v','.mov','.ts'}
EXTRAS = re.compile(r'剧场版|导演剪辑|花絮|彩蛋|加更|预告|先导|番外|特别篇|合集|合辑')


def now(): return datetime.now(timezone.utc).isoformat()


def hint(candidate):
    text = candidate['source_path'].upper()
    tier = 4 if re.search(r'8K|4320P',text) else 3 if re.search(r'4K|2160P',text) else 2 if re.search(r'2K|1440P',text) else 1 if '1080' in text else 0
    return (-tier, -int(candidate['size']), candidate['candidate_id'])


def clean_component(value):
    if not isinstance(value,str) or not value or re.search(r'[<>:"/\\|?*\x00-\x1f]',value) or value.endswith((' ','.')):
        raise ValueError('Unsafe media path component')
    return value


class Worker:
    def __init__(self, root='./instance', *, provider=None, openlist=None,
                 aria2=None, jellyfin=None, secret_store=None, url_probe=probe_url,
                 file_probe=probe_file, clock=time.time):
        self.root = Path(root)
        self.config = json.loads((self.root/'config/shows.json').read_text(encoding='utf-8-sig'))
        self.shows = {s['show_id']:s for s in self.config['shows']}
        self.store = Store(self.root/'state')
        self.secret_store = secret_store or SecretStore(self.root/'secrets/service.dpapi')
        secrets = self.secret_store.load() if any(v is None for v in (provider,openlist,aria2)) else {}
        services=self.config.get('services',{})
        mount=self.config.get('openlist_mount','/Baidu')
        cloud_root=self.config.get('cloud_root','/HomeMediaFollow')
        self.openlist = openlist or OpenList(secrets.get('openlist',{}),services.get('openlist','http://127.0.0.1:5244'), mount=mount, cloud_root=cloud_root)
        self.aria2 = aria2 or Aria2(secrets.get('aria2',{}).get('secret',''),services.get('aria2','http://127.0.0.1:6800'),mount=mount)
        self.jellyfin = jellyfin or (Jellyfin(secrets['jellyfin']['api_key'],services.get('jellyfin','http://127.0.0.1:8096')) if secrets.get('jellyfin',{}).get('api_key') else None)
        self.provider = provider or BaiduAdapter(self.secret_store.baidu_session, intent_sink=self._transfer_intent,cloud_root=cloud_root)
        self.cloud = CloudVersions(self.store,self.provider,self.openlist,mount=mount)
        ffprobe=self.config.get('ffprobe')
        self.url_probe=(lambda *a,**kw: url_probe(*a,ffprobe=ffprobe,**kw)) if ffprobe else url_probe
        self.file_probe=(lambda *a,**kw: file_probe(*a,ffprobe=ffprobe,**kw)) if ffprobe else file_probe
        self.clock=clock
        self._transfer_owner = None
        self._publisher = None

    def report(self, phase, **fields):
        atomic_json(self.root/'reports/latest.json',{'status':phase,'updated_at':now(),**fields})

    def import_observed(self):
        marker = 'observed-import-v1'
        if marker in self.store.state['operations']: return
        path = self.root/'config/observed.json'
        if path.exists():
            observations = json.loads(path.read_bytes())['observations']
            for item in observations:
                identity = hashlib.sha256((item['show_id']+item['cloud_path']+str(item['size'])).encode()).hexdigest()
                self.store.state['inventory']['import:'+identity] = {**item,'record_type':'import','desired_locations':[]}
        else: observations=[]
        self.store.state['operations'][marker] = {'kind':'inventory_import','status':'complete','count':len(observations),'queued':0}
        self.store.event('inventory_imported_without_downloads',{'count':len(observations),'queued':0})

    def inbox(self):
        for path in ordered_inbox(self.root,set(self.shows)):
            envelope = json.loads(path.read_bytes()); validate_request(envelope,set(self.shows))
            request_id = envelope['request_id']
            existing = self.store.state['requests'].get(request_id)
            self.store.register_request(envelope)
            if existing and existing.get('status') == 'applied': continue
            action = envelope['action']
            for show_id in envelope['show_ids']:
                sub = self.store.state['subscriptions'].setdefault(show_id,{'show_id':show_id,'assets':{}})
                if action == 'activate_latest':
                    sub.update(status='active',request_id=request_id,needs_activation=True)
                elif action == 'resume':
                    sub.update(status='active',request_id=request_id)
                    if not sub.get('activation'): sub['needs_activation']=True
                else:
                    sub.update(status='paused' if action=='pause' else 'cancelled')
                    # Subscription controls affect future work; previously submitted
                    # downloads retain their authorization to finish and publish.
            self.store.state['requests'][request_id].update(status='applied',applied_at=now())
            self.store.event('request_applied',{'request_id':request_id,'action':action})
        # Immutable inbox files remain as idempotency receipts.

    def _transfer_intent(self, intent):
        if not self._transfer_owner: raise ValueError('Transfer requires candidate ownership')
        show_id,cid = self._transfer_owner
        entry = {**intent,'kind':'cloud_transfer','show_id':show_id,'candidate_id':cid}
        self.store.state['operations'][intent['operation_id']] = entry
        candidate = self.store.state['inventory'][cid]
        candidate['transfer_operation_id']=intent['operation_id']
        self.store.event('transfer_'+intent['stage'],{'operation_id':intent['operation_id'],'show_id':show_id,'candidate_id':cid})

    def _source(self, show, sub):
        source = sub.get('source_override',show['source'])
        codes = self.secret_store.load().get('extraction_codes',{})
        context = self.provider.verify_share(source['short_url'],codes.get(show['show_id']))
        # A rotated share must still expose the verified resource root.
        roots = self.provider.list_share(context,'/')
        matching=[r for r in roots if r['is_dir'] and (r['id']==source['source_root_id'] if source.get('source_root_id') else r['path']==source['source_root'])]
        if len(matching)!=1:
            raise ValueError('Verified source root changed; explicit rebind required')
        if matching[0]['path']!=source['source_root']:
            source={**source,'source_root':matching[0]['path']}
            sub['source_override']=source
            self.store.event('native_source_path_canonicalized',{'show_id':show['show_id'],'source_root_id':matching[0]['id']})
        rows = self.provider.walk_share(context,source['source_root'])
        candidates=[]
        for row in rows:
            if row['is_dir'] or Path(row['name']).suffix.lower() not in VIDEO or EXTRAS.search(row['path']): continue
            parsed = parse_source_episode(show,row,source['source_root'])
            if not parsed: continue
            candidate = {'record_type':'candidate','show_id':show['show_id'],**parsed,
                'source_id':row['id'],'source_path':row['path'],'name':row['name'],'size':row['size'],
                'provider_fingerprint':row.get('provider_fingerprint'),'source_priority':100}
            cid = candidate_identity(candidate);candidate['candidate_id']=cid
            existing = self.store.state['inventory'].get(cid,{})
            existing.update(candidate);self.store.state['inventory'][cid]=existing;candidates.append(existing)
        if not candidates: raise ValueError('Complete source scan has no regular episodes')
        regular_keys={logical_key(c) for c in candidates}
        previous_candidates=[self.store.state['inventory'][cid] for cid in sub.get('scan_candidates',[])
            if cid in self.store.state['inventory']]
        for key in list(sub.get('pending_quality',{})):
            if key in regular_keys: continue
            old=[c for c in previous_candidates if logical_key(c)==key]
            # Clear only positively identified extras after a complete source
            # scan. A missing regular episode remains pending, never discarded.
            if old and all(EXTRAS.search(c['source_path']) for c in old):
                sub['pending_quality'].pop(key)
                for c in old: c.update(status='excluded',exclusion_reason='non_regular_extra')
                self.store.event('non_regular_pending_excluded',{'show_id':show['show_id'],'logical_key':key,
                    'candidate_ids':[c['candidate_id'] for c in old]})
        digest = hashlib.sha256(json.dumps([(c['candidate_id'],c['size']) for c in sorted(candidates,key=lambda c:c['candidate_id'])]).encode()).hexdigest()
        scan_id = show['show_id']+':'+digest
        if sub.get('needs_activation'):
            sub['activation']=activation(candidates,scan_id);sub['needs_activation']=False
            self.store.event('subscription_activated',{'show_id':show['show_id'],'request_id':sub['request_id'],'activation':sub['activation']})
        sub.update(last_scan_at=self.clock(),scan_id=scan_id,source_status='complete')
        sub['scan_candidates']=[c['candidate_id'] for c in candidates]
        self.store.event('source_scan_complete',{'show_id':show['show_id'],'scan_id':scan_id,'count':len(candidates)})
        return context,candidates

    @staticmethod
    def _same(source, target):
        return (source['name']==target['name'] and source['size']==target['size']
                and bool(source.get('provider_fingerprint'))
                and source['provider_fingerprint']==target.get('provider_fingerprint'))

    def _ensure_cloud(self, show, candidate, context, targets):
        if candidate['candidate_id'] in candidate_quarantine_holds(self.store.state,show['show_id']):
            return False
        # A move response can be lost after the provider committed it. Recover
        # exact owned IDs at the operation's known destination before consulting
        # the original transfer directory, which may now be empty.
        for operation in self.store.state['operations'].values():
            if operation.get('kind')!='cloud_replacement' or operation.get('show_id')!=show['show_id']: continue
            expected=operation.get('new',{})
            if candidate.get('cloud',{}).get('id')!=expected.get('id'): continue
            destination=operation['root']+'/'+PurePosixPath(expected['path']).name
            moved=next((r for r in targets if r['path']==destination and r['id']==expected['id'] and r['size']==candidate['size']),None)
            if moved:
                candidate.update(cloud=moved,managed_cloud=True)
                return True
        if candidate.get('cloud'):
            current = next((r for r in self.provider.list_target(str(PurePosixPath(candidate['cloud']['path']).parent)) if r['id']==candidate['cloud']['id']),None)
            if current and current['size']==candidate['size']:
                candidate['cloud']=current; return True
            candidate.pop('cloud',None)
        operation_id = candidate.get('transfer_operation_id')
        if operation_id:
            intent = self.store.state['operations'][operation_id]
            result = self.provider.reconcile_transfer(intent)
            if result['status']!='confirmed':
                candidate['status']='transfer_unknown';return False
            candidate.update(cloud=result['files'][0],managed_cloud=True)
            intent['status']='confirmed';self.store.event('transfer_reconciled',{'operation_id':operation_id})
            return True
        same = next((r for r in targets if self._same(candidate,r)),None)
        if same:
            trusted=any(r.get('record_type')=='import' and r.get('show_id')==show['show_id']
                and r.get('cloud_path')==same['path'] and r.get('size')==same['size']
                and PurePosixPath(r.get('source_path','')).name==candidate['name'] and r.get('source_id')==candidate['source_id']
                and bool(r.get('provider_fingerprint')) and r['provider_fingerprint']==candidate.get('provider_fingerprint')
                for r in self.store.state['inventory'].values())
            candidate.update(cloud=same,managed_cloud=trusted)
            if trusted:
                self.store.event('imported_cloud_identity_bound',{'candidate_id':candidate['candidate_id'],'cloud_id':same['id']})
            return True
        # Every unverified new version stays separate from the active cloud
        # library. Promotion remains inside this same registered show directory.
        self.cloud.mkdir(show['cloud_path']+'/_follow_candidates')
        destination=show['cloud_path']+'/_follow_candidates/'+candidate['candidate_id'][:24]
        self.cloud.mkdir(destination)
        self._transfer_owner=(show['show_id'],candidate['candidate_id'])
        try:
            result = self.provider.transfer_selected(context,[candidate['source_id']],destination)
            if result['status']!='confirmed': return False
            candidate.update(cloud=result['files'][0],managed_cloud=True)
            self.store.state['operations'][candidate['transfer_operation_id']]['status']='confirmed'
            self.store.event('cloud_candidate_verified',{'candidate_id':candidate['candidate_id'],'show_id':show['show_id']})
            return True
        except TransferUnknown as error:
            self._backoff(error)
            candidate['status']='transfer_unknown';self.store.event('transfer_unknown',{'candidate_id':candidate['candidate_id']});return False
        finally: self._transfer_owner=None

    def _probe_candidate(self, show, candidate, context, targets):
        probe = candidate.get('probe',{})
        if probe.get('status')=='verified': return
        if candidate.get('next_probe_at',0)>self.clock(): return
        if not self._ensure_cloud(show,candidate,context,targets): return
        path = candidate['cloud']['path'];parent=str(PurePosixPath(path).parent)
        self.openlist.list(self.config.get('openlist_mount','/Baidu')+str(PurePosixPath(parent).parent),refresh=True)
        self.openlist.list(self.config.get('openlist_mount','/Baidu')+parent,refresh=True)
        info = self.openlist.get(self.config.get('openlist_mount','/Baidu')+path)
        if info.get('size')!=candidate['size']: raise ValueError('OpenList cloud size differs')
        result = self.url_probe(self.openlist.probe_url(self.config.get('openlist_mount','/Baidu')+path),headers={'User-Agent':'pan.baidu.com'})
        candidate['probe']=result;candidate['probe_attempts']=candidate.get('probe_attempts',0)+1
        delay=min(self.config.get('quality_retry_max_seconds',86400), self.config.get('quality_retry_seconds',900)*2**min(candidate['probe_attempts']-1,6))
        candidate['next_probe_at']=self.clock()+delay
        self.store.event('candidate_probed',{'show_id':show['show_id'],'candidate_id':candidate['candidate_id'],'status':result['status']})

    def _destination(self, show, candidate, current=None):
        if current: return Path(current['destination'])
        title=clean_component(show['title']);suffix=Path(candidate['name']).suffix.lower()
        if show['media_kind']=='variety':
            from .publisher import variety_display_episode
            label=self._publication_label(show,candidate)
            display_episode=variety_display_episode(candidate['episode'],candidate.get('part'))
            return Path(self.config['media_root'])/'综艺'/title/f"Season {candidate['season']:02d}"/(f"{title} - S{candidate['season']:02d}E{display_episode:02d} - {label}"+suffix)
        category='动漫' if show['media_kind']=='anime' else '电视剧'
        return Path(self.config['media_root'])/category/title/f"Season {candidate['season']:02d}"/(f"{title} - S{candidate['season']:02d}E{candidate['episode']:02d}"+suffix)

    def _publication_label(self, show, candidate):
        unit='期' if show['media_kind']=='variety' else '集'
        part=candidate.get('part') or ''
        prefix=''
        if show['media_kind']=='variety':
            name=candidate.get('name','')
            date=re.search(r'(?<!\d)(20\d{2})[.年-](\d{1,2})[.月-](\d{1,2})(?:日)?',name)
            if date: prefix='.'.join(date.groups())+' '
            match=re.search(r'第?\d+期[ -]*([上下])',name)
            if not part and match: part=match.group(1)
        return prefix+f"第{candidate['episode']}{unit}{part}"

    def publisher(self):
        if self._publisher is None:
            if self.config.get('publisher'):
                spec=importlib.util.spec_from_file_location('_explicit_publisher',self.config['publisher'])
                self._publisher=importlib.util.module_from_spec(spec);spec.loader.exec_module(self._publisher)
            else:
                from . import publisher
                self._publisher=publisher
        return self._publisher

    def _trusted_local_receipt(self, show, candidate, destination):
        receipt_root=Path(self.config.get('legacy_receipts',self.root/'state/legacy-receipts'))
        key=lambda p:ntpath.normcase(ntpath.normpath(str(p)))
        expected=self.config.get('openlist_mount','/Baidu')+show['cloud_path']+'/'+candidate['name']
        for path in receipt_root.glob('*.json'):
            try:
                value=json.loads(path.read_text(encoding='utf-8-sig'))
                for entry in value.get('files',[]):
                    if (entry.get('status')=='published' and key(entry.get('destination',''))==key(destination)
                            and entry.get('cloud')==expected and entry.get('bytes')==candidate['size']):
                        # This is a source-bound receipt from the existing publisher,
                        # not a new size-based inference from an arbitrary video.
                        return {'receipt':path.name,'cloud':expected}
            except (ValueError,TypeError,AttributeError,OSError):
                continue
        return None

    def _record_local_removal(self, show_id, key, sub):
        if key in sub.get('local_suppressions',{}): return True
        asset=sub.get('assets',{}).get(key)
        if not asset or not asset.get('destination'): return False
        try:
            Path(asset['destination']).stat()
            return False
        except FileNotFoundError:
            pass
        except OSError:
            return True  # An inaccessible current file never authorizes a refill.
        # Interrupted same-episode publication may temporarily move the old file.
        for job in self.store.state['downloads'].values():
            if job.get('show_id')!=show_id or job.get('logical_key')!=key: continue
            if job.get('status') in {'complete','cancelled','rejected'}: continue
            operation=self.store.state['operations'].get('local-'+job['download_id'][:32],{})
            if operation.get('kind')=='replacement' and operation.get('phase') in {
                    'backup_intent','backed_up','install_intent','switched','verified','rollback_intent'}:
                return False
        receipt=completed_asset_receipt(self.store.state,show_id,key,asset)
        if not receipt or not Path(self.config['media_root']).is_dir(): return True
        sub.setdefault('local_suppressions',{})[key]={
            'reason':'published_local_file_missing','destination':asset['destination'],
            'candidate_id':asset['candidate']['candidate_id'],'completed_download_id':receipt['download_id'],
            'detected_at':now(),'scope':'all_versions_of_this_logical_episode'}
        self.store.event('local_episode_refill_suppressed',{'show_id':show_id,'logical_key':key,
            'completed_download_id':receipt['download_id'],'cloud_inventory_preserved':True})
        return True

    def observe_local_removals(self):
        for sid,sub in self.store.state['subscriptions'].items():
            for key in list(sub.get('assets',{})):
                self._record_local_removal(sid,key,sub)

    def _confirm_suppressed_cloud(self, job):
        operation_id=job.get('cloud_operation_id')
        if not operation_id: return
        operation=self.store.state['operations'].get(operation_id,{})
        if operation.get('phase')=='confirmed':
            job.pop('cloud_confirmation_pending',None);return
        job['cloud_confirmation_pending']=True
        if self.clock()<self.store.state['operations'].get('provider-backoff',{}).get('until',0): return
        try:
            self.cloud.confirm(operation_id)
            job.pop('cloud_confirmation_pending',None)
            self.store.event('suppressed_episode_cloud_confirmed',{'download_id':job['download_id']})
        except Exception as error:
            self._backoff(error)
            self.store.event('suppressed_episode_cloud_pending',{'download_id':job['download_id'],
                'error_type':type(error).__name__})

    def reconcile_suppressed_downloads(self):
        for job in self.store.state['downloads'].values():
            sub=self.store.state['subscriptions'].get(job.get('show_id'),{})
            if job.get('logical_key') not in sub.get('local_suppressions',{}): continue
            if job.get('status')=='cancelled' and job.get('cancellation_reason')=='local_episode_removed_no_refill':
                self._confirm_suppressed_cloud(job);continue
            if job.get('status') in {'complete','cancelled','rejected'}: continue
            try:
                job['status']='cancel_pending'
                self.store.event('local_refill_cancel_intent',{'download_id':job['download_id']})
                if job.get('stage_file'):
                    read_manifest(self.root,job['gid'],job)
                    status=self.aria2.status(job['gid'])
                    if status and status['status'] in {'active','waiting','paused'}:
                        live=self.aria2.call('tellStatus',[job['gid'],['gid','files']])
                        files=live.get('files',[]) if isinstance(live,dict) else []
                        norm=lambda p:ntpath.normcase(ntpath.normpath(str(p)))
                        if (not isinstance(live,dict) or live.get('gid')!=job['gid'] or len(files)!=1
                                or norm(files[0].get('path',''))!=norm(job['stage_file'])):
                            raise ValueError('Refill GID is not bound to its original staging path')
                        self.aria2.call('remove',[job['gid']])
                        status=self.aria2.status(job['gid'])
                        if status and status['status'] in {'active','waiting','paused'}:
                            raise ValueError('Local refill cancellation requires reconciliation')
                    self.aria2.call('saveSession')
                job.update(status='cancelled',cancellation_reason='local_episode_removed_no_refill')
                self.store.event('local_refill_cancelled',{'download_id':job['download_id']})
                self._confirm_suppressed_cloud(job)
            except Exception as error:
                job['last_error']={'type':type(error).__name__,'at':now()}
                self.store.event('local_refill_cancel_pending',{'download_id':job['download_id'],
                    'error_type':type(error).__name__})

    def _queue(self, show, sub, candidate, cloud_operation_id):
        if candidate['candidate_id'] in self._quarantine_holds(show,sub): return
        source=sub.get('source_override',show.get('source',{}))
        if not candidate_in_source_scope(show,candidate,source.get('source_root','/')): return
        key=logical_key(candidate);current=sub['assets'].get(key)
        if self._record_local_removal(show['show_id'],key,sub): return
        if current and not needs_upgrade(current['candidate'],candidate): return
        download_id=hashlib.sha256((sub['request_id']+':'+key+':'+candidate['candidate_id']).encode()).hexdigest()
        if download_id in self.store.state['downloads']: return
        destination=self._destination(show,candidate,current)
        if destination.exists() and not current:
            # A pre-existing publication must have an actual source-bound publisher
            # receipt. Matching length/resolution alone never establishes identity.
            if not self._trusted_local_receipt(show,candidate,destination):
                raise ValueError('Unregistered local target requires explicit association')
            measured=self.file_probe(destination)
            if destination.stat().st_size!=candidate['size'] or not resolution_rank({'probe':measured}) or resolution_rank({'probe':measured})[0]<resolution_rank(candidate)[0]:
                raise ValueError('Existing local target is not a matching managed asset')
            sub['assets'][key]={'candidate':copy.deepcopy(candidate),'destination':str(destination),'sha256':file_sha256(destination),'managed_local':True,'cloud_operation_id':cloud_operation_id}
            job={'download_id':download_id,'gid':download_id[:16],'show_id':show['show_id'],'request_id':sub['request_id'],'logical_key':key,'status':'adopted','destination':str(destination),'candidate':copy.deepcopy(candidate),'cloud_operation_id':cloud_operation_id,'completed_bytes':candidate['size'],'download_speed':0}
            self.store.state['downloads'][download_id]=job;self.store.event('existing_episode_adopted',{'download_id':download_id,'show_id':show['show_id']})
            return
        if current and not current.get('managed_local'):
            raise ValueError('Automatic replacement is limited to managed local versions')
        staging=Path(self.config['staging'])/download_id
        staging.mkdir(parents=True,exist_ok=True);source=staging/clean_component(candidate['name'])
        if source.exists() or source.with_name(source.name+'.aria2').exists():
            raise ValueError('Untracked staging file exists')
        reserved=sum(max(0,j['candidate']['size']-int(j.get('completed_bytes',0))) for j in self.store.state['downloads'].values() if j.get('status') in {'queued','downloading','queue_intent','queue_unknown'})
        if shutil.disk_usage(staging).free < reserved+candidate['size']+self.config.get('min_free_bytes',10*1024**3):
            raise ValueError('Insufficient free space including existing reservations')
        job={'download_id':download_id,'gid':download_id[:16],'show_id':show['show_id'],'request_id':sub['request_id'],'logical_key':key,
            'candidate':copy.deepcopy(candidate),'stage_file':str(source),'destination':str(destination),'status':'queue_intent','created_at':now(),
            'cloud_operation_id':cloud_operation_id,'desired_locations':['cloud','local'],'previous_sha256':current.get('sha256') if current else None}
        self.store.state['downloads'][download_id]=job
        self.store.event('download_intent',{'download_id':download_id,'show_id':show['show_id'],'request_id':sub['request_id']})
        write_manifest(self.root,job)
        try:
            self.aria2.add(job['gid'],self.openlist.download_url(self.config.get('openlist_mount','/Baidu')+candidate['cloud']['path']),str(staging),source.name)
            job['status']='queued'
        except Exception:
            job['status']='queue_unknown';self.store.event('download_submit_unknown',{'download_id':download_id});raise
        self.store.event('download_submitted',{'download_id':download_id,'gid':job['gid']})

    def _retry_metadata(self, job):
        if job.get('metadata_status')!='retry_pending': return True
        current=self.clock()
        if current<job.get('metadata_next_retry_at',0): return False
        with publisher_lock(self.config['publisher_lock']):
            asset=self.store.state['subscriptions'][job['show_id']]['assets'][job['logical_key']]
            if (asset['candidate']['candidate_id']!=job['candidate']['candidate_id']
                    or Path(asset['destination']).resolve()!=Path(job['destination']).resolve()
                    or file_sha256(job['destination'])!=asset['sha256']):
                raise ValueError('Metadata target does not belong to this managed version')
            job['metadata_attempts']=job.get('metadata_attempts',0)+1
            job['metadata_next_retry_at']=current+self.config.get('recovery_interval_seconds',900)
            try:
                publisher=self.publisher()
                publisher.write_metadata(Path(job['destination']),self._publication_label(self.shows[job['show_id']],job['candidate']))
                publisher.create_poster(Path(job['destination']))
            except Exception as error:
                job['metadata_error_type']=type(error).__name__
                self.store.event('episode_metadata_pending',{'download_id':job['download_id'],'error_type':type(error).__name__})
                return False
        job['metadata_status']='written'
        job.pop('metadata_error_type',None)
        job.pop('metadata_next_retry_at',None)
        job['next_jellyfin_refresh_at']=0
        self.store.event('episode_metadata_written',{'download_id':job['download_id']})
        return True

    def _verify_jellyfin(self, job):
        if not self._retry_metadata(job):
            job['jellyfin']={'status':'waiting_for_metadata','playable':False};return
        if not self.jellyfin:
            job['jellyfin']={'status':'authentication_required'};return
        current=self.clock()
        if current>=job.get('next_jellyfin_refresh_at',0):
            job['next_jellyfin_refresh_at']=current+self.config.get('recovery_interval_seconds',900)
            self.store.event('jellyfin_refresh_attempt',{'download_id':job['download_id']})
            refresh=self.jellyfin.refresh(job['destination'])
            if isinstance(refresh,dict) and refresh.get('status')=='requested':
                job['refresh_requested_at']=now();job['refresh_status']='requested'
                self.store.event('jellyfin_refresh_requested',{'download_id':job['download_id']})
            else:
                job['refresh_status']='pending'
                self.store.event('jellyfin_refresh_deferred',{'download_id':job['download_id']})
        result=self.jellyfin.verify(job['destination']);job['jellyfin']=result
        expected=resolution_rank(job['candidate'])
        measured=resolution_rank({'probe':{'status':'verified','width':result.get('width'),'height':result.get('height')}})
        if result.get('status')=='verified' and result.get('playable') and measured and measured[0]>=expected[0]:
            with publisher_lock(self.config['publisher_lock']):
                asset=self.store.state['subscriptions'][job['show_id']]['assets'][job['logical_key']]
                if asset['candidate']['candidate_id']!=job['candidate']['candidate_id'] or file_sha256(job['destination'])!=asset['sha256']:
                    raise ValueError('Playback result does not belong to this managed version')
                manifest={**job,'stage_file':job['destination']}
                if gate(manifest,job['destination'],prober=self.file_probe)['status']!='verified':
                    raise ValueError('Published media changed before confirmation')
            if job.get('replacement_operation_id'): Replacement(self.store,job['replacement_operation_id']).confirm()
            self.cloud.confirm(job.get('cloud_operation_id'))
            job['status']='complete';job['completed_at']=now()
            job['completed_bytes']=job['candidate']['size'];job['download_speed']=0
            job.pop('last_error',None)
            self.store.event('episode_playback_verified',{'download_id':job['download_id'],'show_id':job['show_id'],'item_id':result.get('item_id')})

    def _retry_download(self, job, sub, observed):
        if sub.get('status')!='active': return
        if self.clock()<self.store.state['operations'].get('provider-backoff',{}).get('until',0): return
        if job.get('retry_attempts',0)>=5: return
        if not job.get('next_retry_at'):
            job['next_retry_at']=self.clock()+self.config.get('recovery_interval_seconds',900)
            self.store.event('download_retry_scheduled',{'download_id':job['download_id']});return
        if self.clock()<job['next_retry_at']: return
        read_manifest(self.root,job['gid'],job)
        expected=job['candidate']['cloud'];parent=str(PurePosixPath(expected['path']).parent)
        live=next((r for r in self.provider.list_target(parent) if r['path']==expected['path']),None)
        fingerprint=expected.get('provider_fingerprint') or job['candidate'].get('provider_fingerprint')
        if (not live or live['id']!=expected['id'] or live['size']!=job['candidate']['size']
                or fingerprint and live.get('provider_fingerprint')!=fingerprint):
            raise ValueError('Retry cloud identity differs from the frozen download')
        source=Path(job['stage_file'])
        if source.exists() and not source.with_name(source.name+'.aria2').exists():
            raise ValueError('Unfinished download data lacks its Aria2 resume control')
        job['retry_attempts']=job.get('retry_attempts',0)+1
        job['next_retry_at']=self.clock()+min(86400,900*2**job['retry_attempts'])
        self.store.event('download_retry_intent',{'download_id':job['download_id'],'attempt':job['retry_attempts']})
        if observed and observed['status'] in {'error','removed'}:
            self.aria2.call('removeDownloadResult',[job['gid']])
        self.openlist.list(self.config.get('openlist_mount','/Baidu')+str(PurePosixPath(job['candidate']['cloud']['path']).parent),refresh=True)
        self.aria2.add(job['gid'],self.openlist.download_url(self.config.get('openlist_mount','/Baidu')+job['candidate']['cloud']['path']),str(source.parent),source.name)
        job['status']='queued'
        self.store.event('download_retry_submitted',{'download_id':job['download_id'],'attempt':job['retry_attempts']})

    def _backoff(self, error):
        if getattr(error,'code',None)!=429: return
        until=self.clock()+max(1800,getattr(error,'retry_after',0) or 0)
        self.store.state['operations']['provider-backoff']={'kind':'provider_backoff','until':until}
        self.store.event('provider_backoff',{'until':until})

    def completions(self):
        for job in list(self.store.state['downloads'].values()):
            if job.get('status') in {'complete','cancelled','rejected','paused'}: continue
            sub=self.store.state['subscriptions'].get(job['show_id'],{})
            if not sub: continue
            if job.get('logical_key') in sub.get('local_suppressions',{}): continue
            try:
                if job['status'] in {'published','adopted'}:
                    self._verify_jellyfin(job);continue
                operation_id='local-'+job['download_id'][:32]
                operation=self.store.state['operations'].get(operation_id)
                receipt_path=self.root/'state/hook-receipts'/(job['gid']+'.json')
                completed_receipt=json.loads(receipt_path.read_bytes()) if receipt_path.exists() else {}
                # Publication recovery is owned by the durable transaction, not by
                # Aria2's volatile finished-task list.
                if operation and operation.get('phase')!='rolled_back':
                    observed={'status':'complete'}
                elif completed_receipt.get('status')=='verified' and completed_receipt.get('gid')==job['gid']:
                    observed={'status':'complete'}
                else:
                    observed=self.aria2.status(job['gid'])
                if observed is None:
                    if job['status']=='queue_intent':
                        if sub.get('status')!='active':
                            job['deferred_reason']='subscription_not_active';continue
                        source=Path(job['stage_file'])
                        if source.exists() or source.with_name(source.name+'.aria2').exists():
                            raise ValueError('Unsubmitted download has unexpected staging data')
                        write_manifest(self.root,job)
                        self.aria2.add(job['gid'],self.openlist.download_url(self.config.get('openlist_mount','/Baidu')+job['candidate']['cloud']['path']),str(source.parent),source.name)
                        job['status']='queued';self.store.event('download_intent_recovered',{'download_id':job['download_id']})
                    else:
                        job['status']='queue_unknown';self._retry_download(job,sub,observed)
                    continue
                job['completed_bytes']=int(observed.get('completedLength',job.get('completed_bytes',0)))
                job['download_speed']=int(observed.get('downloadSpeed',job.get('download_speed',0)))
                if observed['status'] in {'active','waiting','paused'}:
                    job['status']='downloading' if observed['status']=='active' else 'queued';continue
                if observed['status']!='complete':
                    job.update(status='download_error',error_code=observed.get('errorCode'))
                    self._retry_download(job,sub,observed);continue
                source=Path(job['stage_file'])
                if operation and operation.get('phase') in {'verified','confirmed'}:
                    if not Path(job['destination']).is_file() or file_sha256(job['destination'])!=operation['candidate_sha256']:
                        raise ValueError('Published managed target changed')
                else:
                    manifest=read_manifest(self.root,job['gid'],job)
                    if operation and operation.get('phase') not in {'prepared','rolled_back'}:
                        with publisher_lock(self.config['publisher_lock']):
                            operation=Replacement(self.store,operation_id).recover(lambda p: gate({**manifest,'stage_file':str(p)},p,prober=self.file_probe)['status']=='verified')
                        if operation['phase'] not in {'verified','confirmed'}: raise ValueError('Local publication requires recovery')
                    else:
                        result=gate(manifest,source,prober=self.file_probe)
                        atomic_json(self.root/'state/hook-receipts'/(job['gid']+'.json'),{'gid':job['gid'],**result})
                        if result['status']!='verified':
                            job.update(status='rejected',gate=result);self.store.event('local_media_rejected',{'download_id':job['download_id']});continue
                        with publisher_lock(self.config['publisher_lock']):
                            replacement=Replacement(self.store,operation_id)
                            replacement.prepare(source,job['destination'],job.get('previous_sha256'),metadata={'backup_root':str(Path(self.config['recovery_root'])/operation_id),'show_id':job['show_id'],'request_id':job['request_id']})
                            replacement.operation.update(show_id=job['show_id'],request_id=job['request_id'])
                            operation=replacement.run(lambda p: gate({**manifest,'stage_file':str(p)},p,prober=self.file_probe)['status']=='verified')
                            if operation['phase']!='verified': raise ValueError('Local publication did not verify')
                job['metadata_status']='retry_pending'
                job.update(status='published',replacement_operation_id=operation_id,published_at=now(),completed_bytes=job['candidate']['size'],download_speed=0)
                sub['assets'][job['logical_key']]={'candidate':copy.deepcopy(job['candidate']),'destination':job['destination'],'sha256':operation['candidate_sha256'],'managed_local':True,'cloud_operation_id':job.get('cloud_operation_id')}
                self.store.event('episode_published',{'download_id':job['download_id'],'show_id':job['show_id'],'destination':job['destination']})
                self._verify_jellyfin(job)
            except Exception as error:
                self._backoff(error)
                failed=self.store.state['operations'].get('local-'+job['download_id'][:32],{})
                if failed.get('phase')=='rolled_back':
                    job.update(status='rejected',rejection_reason='replacement_rolled_back')
                job['last_error']={'type':type(error).__name__,'at':now()}
                self.store.event('download_reconciliation_pending',{'download_id':job['download_id'],'error_type':type(error).__name__})

    def _scan_due(self, sub, force=False):
        if force or sub.get('needs_activation'): return True
        current=self.clock()
        interval=self.config.get('scan_interval_seconds',86400)
        if isinstance(interval,bool) or not isinstance(interval,int) or not 900<=interval<=86400:
            raise ValueError('Invalid source scan interval')
        if interval<86400:
            # Completed scans consume their time slot; recovery ticks do not.
            # Use the existing successful-scan receipt, never reset activation.
            completed=sub.get('last_scan_at')
            if isinstance(completed,(int,float)) and not isinstance(completed,bool):
                if completed//interval>=current//interval:
                    return False
                if completed>=sub.get('last_scan_attempt',0):
                    return True  # A successful late-hour scan must not delay the next hour.
            return current-sub.get('last_scan_attempt',0)>=self.config.get('recovery_interval_seconds',900)
        local=datetime.fromtimestamp(current,timezone(timedelta(hours=8)))
        due=(local-timedelta(days=1) if local.hour<self.config.get('scan_hour',18) else local).date().isoformat()
        if sub.get('last_daily_scan','')>=due: return False
        return current-sub.get('last_scan_attempt',0)>=self.config.get('recovery_interval_seconds',900)

    def _catalog(self, shows):
        if self.config.get('catalog_mode','static') == 'static':
            return
        record=self.store.state['operations'].setdefault('catalog-check',{'kind':'catalog_check'})
        if self.clock()<record.get('backoff_until',0): return
        catalog=Catalog(backoff_until=record.get('backoff_until',0),catalog_url=self.config['catalog_url'])
        effective=[]
        active_codes=self.secret_store.load().get('extraction_codes',{})
        for show in shows:
            sub=self.store.state['subscriptions'][show['show_id']]
            source=dict(sub.get('source_override',show['source']))
            if sub.get('catalog',{}).get('binding_hash'): source['catalog_binding_hash']=sub['catalog']['binding_hash']
            source['pwd']=active_codes.get(show['show_id'])  # memory only; never stored in status/config
            effective.append({**show,'source':source})
        findings=catalog.check(effective)
        for show in shows:
            finding=findings.get(show['show_id'],{})
            sub=self.store.state['subscriptions'][show['show_id']]
            old_hash=sub.get('catalog',{}).get('binding_hash')
            sub['catalog']={k:v for k,v in finding.items() if k in {'status','evidence_hash'}}
            if old_hash: sub['catalog']['binding_hash']=old_hash
            if finding.get('status')=='unchanged' and finding.get('binding_hash'):
                sub['catalog']['binding_hash']=finding['binding_hash']
            elif finding.get('binding_hash'):
                sub['catalog']['proposed_binding_hash']=finding['binding_hash']
            binding=finding.get('binding')
            if finding.get('status')=='changed' and binding:
                context=self.provider.verify_share(binding['short_url'],binding.get('pwd'))
                roots=self.provider.list_share(context,'/')
                original=sub.get('source_override',show['source'])
                if not any(r['is_dir'] and (r['id']==original['source_root_id'] if original.get('source_root_id') else r['path']==original['source_root']) for r in roots):
                    sub['catalog']['status']='source_rebind_required';continue
                codes=self.secret_store.load().get('extraction_codes',{});codes[show['show_id']]=binding.get('pwd')
                self.secret_store.update({'extraction_codes':codes})
                sub['source_override']={**original,'short_url':binding['short_url'],'catalog_binding_hash':finding['binding_hash']}
                sub['catalog']['binding_hash']=finding['binding_hash']
                sub['catalog'].pop('proposed_binding_hash',None)
                self.store.event('source_binding_updated',{'show_id':show['show_id'],'evidence_hash':finding.get('evidence_hash')})
        record.update(checked_at=now(),backoff_until=catalog.backoff_until)
        self.store.event('catalog_checked',{'shows':len(shows),'backoff_until':catalog.backoff_until})

    def _quarantine_holds(self, show, sub):
        holds=candidate_quarantine_holds(self.store.state,show['show_id'])
        if sub.get('candidate_quarantine_holds',{})!=holds:
            if holds: sub['candidate_quarantine_holds']=holds
            else: sub.pop('candidate_quarantine_holds',None)
            self.store.event('candidate_quarantine_hold_changed',{'show_id':show['show_id'],'holds':holds})
        return holds

    def reconcile_pending_transfers(self):
        """Read existing intents independently of selection, including paused shows."""
        counts={'attempted':0,'confirmed':0,'pending':0,'skipped':0,'deferred':False}
        with self.store.locked():
            state=self.store.state
            if self.clock()<state['operations'].get('provider-backoff',{}).get('until',0):
                counts['deferred']=True;return counts
            limit=max(1,min(100,int(self.config.get('pending_transfer_reconcile_limit',100))))
            pending=pending_cloud_transfers(state)
            ordered=sorted(pending.items(),key=lambda pair:(pair[1].get('last_reconcile_at',0),pair[0]))
            for operation_id,operation in ordered:
                if counts['attempted']>=limit: break
                cid=operation.get('candidate_id');show=self.shows.get(operation.get('show_id'))
                candidate=state['inventory'].get(cid)
                if not show or not isinstance(candidate,dict): counts['skipped']+=1;continue
                known_ids={str(row['id']) for row in [candidate.get('cloud',{}),*operation.get('receipt',{}).get('items',[])] if row.get('id')}
                retired=bool(candidate.get('retired_by')) or any(
                    item.get('kind')=='cloud_replacement' and item.get('phase')=='confirmed'
                    and item.get('show_id')==show['show_id'] and isinstance(item.get('old'),dict)
                    and str(item['old'].get('id')) in known_ids and item['old'].get('size')==candidate.get('size')
                    and bool(candidate.get('provider_fingerprint')) and item['old'].get('provider_fingerprint')==candidate['provider_fingerprint']
                    for item in state['operations'].values())
                if retired or cid in candidate_quarantine_holds(state,show['show_id']): counts['skipped']+=1;continue
                counts['attempted']+=1;operation['last_reconcile_at']=self.clock()
                try:
                    target=operation.get('target');files=operation.get('files',[])
                    fingerprint=candidate.get('provider_fingerprint')
                    if (operation.get('operation_id',operation_id)!=operation_id or candidate.get('candidate_id')!=cid
                            or candidate.get('show_id')!=show['show_id'] or candidate.get('transfer_operation_id')!=operation_id
                            or target not in {show['cloud_path'],show['cloud_path']+'/_follow_candidates/'+cid[:24]}
                            or not isinstance(files,list) or len(files)!=1 or not fingerprint):
                        raise ValueError('Transfer intent binding differs')
                    source=files[0]
                    if (str(source.get('id'))!=str(candidate.get('source_id')) or source.get('size')!=candidate.get('size')
                            or source.get('path')!=candidate.get('source_path') or source.get('name')!=candidate.get('name')
                            or source.get('provider_fingerprint')!=fingerprint
                            or PurePosixPath(candidate['name']).name!=candidate['name']):
                        raise ValueError('Transfer source identity differs')
                    result=self.provider.reconcile_transfer(copy.deepcopy(operation))
                    observed=result.get('files',[])
                    if result.get('status')!='confirmed' or not isinstance(observed,list) or len(observed)!=1:
                        raise CloudPending('Transfer outcome remains unknown')
                    row=observed[0];path=target+'/'+candidate['name']
                    receipts=operation.get('receipt',{}).get('items',[])
                    if (not re.fullmatch(r'[0-9]{1,32}',str(row.get('id',''))) or row.get('is_dir') is not False
                            or row.get('path')!=path or row.get('name')!=candidate['name']
                            or row.get('size')!=candidate['size'] or row.get('provider_fingerprint')!=fingerprint
                            or 'before_ids' not in operation or str(row['id']) in set(map(str,operation['before_ids']))
                            or receipts and (len(receipts)!=1 or receipts[0].get('path')!=path or str(receipts[0].get('id'))!=str(row['id']))):
                        raise ValueError('Transfer target identity differs')
                    existing=candidate.get('cloud')
                    if existing and any(existing.get(field)!=row.get(field) for field in ('id','path','size','provider_fingerprint')):
                        raise ValueError('Existing cloud pointer differs')
                    if not existing: candidate['cloud']=copy.deepcopy(row)
                    candidate['managed_cloud']=True
                    if candidate.get('status')=='transfer_unknown': candidate.pop('status')
                    operation.update(status='confirmed',reconciled_at=now())
                    if 'last_reconcile_error' in operation: operation['resolved_reconcile_error']=operation.pop('last_reconcile_error')
                    self.store.event('pending_transfer_reconciled',{'operation_id':operation_id,'candidate_id':cid,'cloud_id':row['id']})
                    counts['confirmed']+=1
                except Exception as error:
                    operation['status']='unknown'
                    operation['last_reconcile_error']={'type':type(error).__name__,'at':now(),'provider_code':getattr(error,'code',None)}
                    self._backoff(error)
                    self.store.event('pending_transfer_unresolved',{'operation_id':operation_id,'error_type':type(error).__name__})
                    counts['pending']+=1
                    if getattr(error,'code',None)==429: counts['deferred']=True;break
        return counts

    def _process_show(self, show, sub, force):
        holds=self._quarantine_holds(show,sub)
        active=sub.get('activation')
        pending_keys=pending_cloud_keys(self.store.state,show['show_id'])
        complete_scan=self._scan_due(sub,force)
        if complete_scan:
            sub['last_scan_attempt']=self.clock()
            self.store.event('source_scan_started',{'show_id':show['show_id']})
            context,candidates=self._source(show,sub)
            local=datetime.fromtimestamp(self.clock(),timezone(timedelta(hours=8)))
            sub['last_daily_scan']=(local-timedelta(days=1) if self.config.get('scan_interval_seconds',86400)==86400 and local.hour<self.config.get('scan_hour',18) else local).date().isoformat()
        else:
            candidates=[self.store.state['inventory'][cid] for cid in sub.get('scan_candidates',[]) if cid in self.store.state['inventory'] and cid not in holds]
            if not candidates: return
            # Fresh verification is required before transferring any cached source id.
            source=sub.get('source_override',show['source']);codes=self.secret_store.load().get('extraction_codes',{})
            context=self.provider.verify_share(source['short_url'],codes.get(show['show_id']))
            # Enumerate only directories actually needed below, before selecting a new transfer.
            for directory in sorted({str(PurePosixPath(c['source_path']).parent) for c in candidates if c.get('probe',{}).get('status')!='verified' and (in_requested_range(c,sub['activation']) or logical_key(c) in pending_keys)}):
                page=1
                while True:
                    rows=self.provider.list_share(context,directory,page=page)
                    if len(rows)<100: break
                    page+=1
        source=sub.get('source_override',show.get('source',{}))
        candidates=[candidate for candidate in candidates if candidate['candidate_id'] not in holds
            and candidate_in_source_scope(show,candidate,source.get('source_root','/'))]
        if not candidates: return
        active=sub['activation'];targets=self.provider.list_target(show['cloud_path'])
        self.store.state['inventory']['cloud_inventory:'+show['show_id']]={'record_type':'cloud_inventory','show_id':show['show_id'],'status':'observed','files':targets,'observed_at':now(),'desired_locations':[]}
        grouped=defaultdict(list)
        for candidate in candidates: grouped[logical_key(candidate)].append(candidate)
        ordered=sorted(grouped.items(),key=lambda pair:(not in_requested_range(pair[1][0],active),-pair[1][0]['episode']))
        for key,group in ordered:
            missing=self._record_local_removal(show['show_id'],key,sub)
            requested=local_requested(group[0],sub) and not missing
            if requested and any(j.get('logical_key')==key and j.get('status') not in {'complete','cancelled','rejected'} for j in self.store.state['downloads'].values()):
                sub.setdefault('upgrade_checks_deferred',{})[key]='current_download_not_confirmed'
                continue
            rejected={j.get('candidate',{}).get('candidate_id') for j in self.store.state['downloads'].values()
                if j.get('logical_key')==key and j.get('status')=='rejected'}
            group=[c for c in group if c['candidate_id'] not in rejected]
            if not group:
                sub.setdefault('pending_quality',{})[key]={'reason':'all_candidates_rejected','checked_at':now()};continue
            previous=self.store.state['inventory'].get('selection:'+key)
            if not requested and not complete_scan and key not in pending_keys: continue
            if not requested and key not in pending_keys and any(any(self._same(c,r) for r in targets) for c in group):
                historical=[r for r in self.store.state['inventory'].values() if r.get('record_type')=='import'
                    and r.get('show_id')==show['show_id'] and r.get('episode')==group[0]['episode'] and r.get('part')==group[0].get('part')]
                fallback=any(resolution_rank({'probe':r.get('historical_probe',{})}) and resolution_rank({'probe':r['historical_probe']})[0]<3 for r in historical)
                if not fallback: continue
            if previous is None:
                # Bind only the previously reviewed cloud-library selection,
                # after live source and target identities agree. This enables
                # historical cloud-only upgrades without requesting local files.
                imported=[r for r in self.store.state['inventory'].values() if r.get('record_type')=='import'
                    and r.get('show_id')==show['show_id'] and r.get('episode')==group[0]['episode'] and r.get('part')==group[0].get('part')]
                old=next((c for c in group for r in imported if c['source_id']==r.get('source_id') and c['size']==r.get('size') and any(self._same(c,t) for t in targets)),None)
                if old:
                    self._probe_candidate(show,old,context,targets)
                    if resolution_rank(old) and old.get('managed_cloud'):
                        previous={'candidate':old,'cloud':old['cloud'],'managed_cloud':True}
            current=sub['assets'].get(key) if requested else None
            for candidate in sorted(group,key=hint):
                self._probe_candidate(show,candidate,context,targets)
                if resolution_rank(candidate) and resolution_rank(candidate)[0]>=3:
                    # Current 4K is stable; do not churn same-tier variants.
                    break
            selected=choose_candidate(group,current['candidate'] if current else (previous['candidate'] if previous else None))
            if not selected:
                sub.setdefault('pending_quality',{})[key]={'reason':'no_verified_1080p_candidate','checked_at':now()};continue
            if current and selected['candidate_id']==current['candidate']['candidate_id']: continue
            if not selected.get('cloud') and not self._ensure_cloud(show,selected,context,targets): continue
            # Adopt a native-identity matching existing cloud copy as the managed current.
            if previous and previous['candidate']['candidate_id']!=selected['candidate_id'] and not needs_upgrade(previous['candidate'],selected):
                raise ValueError('Cloud replacement must strictly improve a verified managed tier')
            self.inbox()
            if sub.get('status')!='active': return
            retirement_previous=previous
            if previous and not previous.get('managed_cloud'):
                sub.setdefault('cloud_conflicts',{})[key]='unmanaged_previous_preserved'
                retirement_previous=None
            cloud_op=self.cloud.select(show,key,selected,retirement_previous)
            if selected['cloud']['path'].startswith(show['cloud_path']+'/'):
                self.openlist.list(self.config.get('openlist_mount','/Baidu')+show['cloud_path'],refresh=True)
            selection={'record_type':'cloud_selection','show_id':show['show_id'],'candidate':copy.deepcopy(selected),'cloud':dict(selected['cloud']),'managed_cloud':bool(selected.get('managed_cloud')),'status':'observed','cloud_operation_id':cloud_op,'desired_locations':['cloud']}
            self.store.state['inventory']['selection:'+key]=selection
            sub.setdefault('pending_quality',{}).pop(key,None)
            if any(c.get('probe',{}).get('status') in {'unknown',None} for c in group) and resolution_rank(selected)[0]<3:
                selection['fallback_reason']='higher_resolution_not_verified'
            self.store.event('cloud_selection_recorded',{'show_id':show['show_id'],'logical_key':key,'candidate_id':selected['candidate_id'],'local_requested':requested})
            if requested:
                self._queue(show,sub,selected,cloud_op)
                if key in sub.get('local_suppressions',{}): self.cloud.confirm(cloud_op)
            else:
                self.cloud.confirm(cloud_op)

    def run(self, force_scan=False):
        with self.store.locked():
            self.import_observed();self.inbox()
            self.observe_local_removals();self.reconcile_suppressed_downloads();self.completions()
            transfer_reconciliation=self.reconcile_pending_transfers()
            due=[self.shows[sid] for sid,sub in self.store.state['subscriptions'].items() if sub.get('status')=='active' and self._scan_due(sub,force_scan)]
            if due:
                try: self._catalog(due)
                except Exception as error:
                    self.store.event('catalog_pending',{'error_type':type(error).__name__})
            for show_id,sub in self.store.state['subscriptions'].items():
                self._quarantine_holds(self.shows[show_id],sub)
                if sub.get('status')!='active': continue
                if self.clock()<self.store.state['operations'].get('provider-backoff',{}).get('until',0): continue
                if not self._scan_due(sub,force_scan) and not source_work_due(self.store.state,sub,self.clock()): continue
                self.report('processing_show',show_id=show_id)
                try:
                    self._process_show(self.shows[show_id],sub,force_scan)
                    sub.pop('last_error',None)
                except Exception as error:
                    self._backoff(error)
                    sub['last_error']={'type':type(error).__name__,'at':now(),'provider_code':getattr(error,'code',None)}
                    self.store.event('show_pending',{'show_id':show_id,'error_type':type(error).__name__})
                self.store.save()
                self.completions()
            self.store.save()
            counts=defaultdict(int)
            for job in self.store.state['downloads'].values(): counts[job['status']]+=1
            self.report('cycle_complete',downloads=dict(counts),subscriptions=len(self.store.state['subscriptions']),jellyfin_authorized=self.jellyfin is not None,
                pending_transfer_reconciliation=transfer_reconciliation,
                local_refill_suppressions={sid:list(sub['local_suppressions']) for sid,sub in self.store.state['subscriptions'].items() if sub.get('local_suppressions')},
                candidate_quarantine_holds={sid:sub['candidate_quarantine_holds'] for sid,sub in self.store.state['subscriptions'].items() if sub.get('candidate_quarantine_holds')})
            return {'status':'cycle_complete','downloads':dict(counts),'subscriptions':len(self.store.state['subscriptions'])}


def main(argv=None):
    parser=argparse.ArgumentParser();parser.add_argument('--root',default='./instance');parser.add_argument('--force-scan',action='store_true')
    args=parser.parse_args(argv)
    try:
        result=Worker(args.root).run(args.force_scan);print(json.dumps(result));return 0
    except Exception as error:
        result={'status':'worker_not_completed','error_type':type(error).__name__}
        try: atomic_json(Path(args.root)/'reports/latest.json',{**result,'updated_at':now()})
        except Exception: pass
        print(json.dumps(result));return 1
