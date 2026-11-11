"""Durable, narrowly owned cloud version switches inside one registered show."""
from pathlib import PurePosixPath
import hashlib


class CloudPending(RuntimeError):
    pass


class CloudVersions:
    def __init__(self, store, provider, openlist, mount="/Baidu"):
        self.store, self.provider, self.openlist = store, provider, openlist
        self.mount=mount

    def _record(self, operation, phase):
        operation['phase'] = phase
        self.store.event('cloud_' + phase, {'operation_id': operation['operation_id']})

    def _find(self, path):
        p = PurePosixPath(path)
        return next((r for r in self.provider.list_target(str(p.parent)) if r['path'] == path), None)

    def mkdir(self, path):
        parent = str(PurePosixPath(path).parent)
        existing = next((r for r in self.provider.list_target(parent) if r['path']==path),None)
        if existing:
            if not existing['is_dir']: raise CloudPending('Cloud directory conflicts with a file')
            return
        self.openlist.fs('mkdir', {'path': self.mount + path})
        self.openlist.list(self.mount + parent, refresh=True)

    def _move(self, operation, field, destination):
        source = operation[field]
        destination = destination.rstrip('/') + '/' + PurePosixPath(source['path']).name
        if source['path'] == destination:
            return
        at_destination = self._find(destination)
        at_source = self._find(source['path'])
        if at_destination and at_destination['id'] == source['id'] and not at_source:
            source['path'] = destination
            self._record(operation, field + '_moved')
            return
        if at_destination or not at_source or at_source['id'] != source['id'] or at_source['size'] != source['size']:
            raise CloudPending('Cloud identity or destination conflict')
        marker = field + '_move_intent'
        if operation.get('phase') == marker:
            # A previous write may still be completing. Do not blindly issue it again.
            raise CloudPending('Unresolved cloud move intent')
        self._record(operation, marker)
        self.openlist.fs('move', {'src_dir':self.mount+str(PurePosixPath(source['path']).parent),
            'dst_dir':self.mount+str(PurePosixPath(destination).parent), 'names':[PurePosixPath(source['path']).name]})
        observed = self._find(destination)
        if not observed or observed['id'] != source['id'] or self._find(source['path']):
            raise CloudPending('Cloud move is awaiting provider reconciliation')
        source['path'] = destination
        self._record(operation, field + '_moved')

    def select(self, show, key, candidate, previous=None):
        """Install only a verified selected cloud asset; no download is created here."""
        if candidate.get('probe',{}).get('status') != 'verified':
            raise ValueError('Verified cloud candidate required')
        old = previous.get('cloud') if previous else None
        new = dict(candidate['cloud'])
        if not candidate.get('managed_cloud'):
            if str(PurePosixPath(new['path']).parent)==show['cloud_path'] and not old:
                return None  # Read reuse never grants retirement ownership.
            raise CloudPending('Unregistered cloud asset cannot be changed')
        if old and old['id'] == new['id']:
            return None
        op_id = 'cloud-' + hashlib.sha256((key+':'+new['id']).encode()).hexdigest()[:24]
        operation = self.store.state['operations'].get(op_id)
        if not operation:
            if old and not previous.get('managed_cloud'):
                raise CloudPending('Unmanaged cloud version cannot be retired')
            operation = {'kind':'cloud_replacement','operation_id':op_id,'show_id':show['show_id'],
                'logical_key':key,'new':new,'old':dict(old) if old else None,
                'root':show['cloud_path'],'phase':'prepared'}
            self.store.state['operations'][op_id] = operation
            self._record(operation, 'prepared')
        if operation['phase'] == 'confirmed':
            candidate['cloud'] = dict(operation['new'])
            return op_id
        if operation['old']:
            recovery = show['cloud_path'] + '/_follow_recovery'
            self.mkdir(recovery)
            recovery += '/' + op_id
            self.mkdir(recovery)
            self._move(operation, 'old', recovery)
        self._move(operation, 'new', show['cloud_path'])
        candidate['cloud'] = dict(operation['new'])
        self._record(operation, 'switched')
        return op_id

    def confirm(self, operation_id):
        if not operation_id:
            return
        operation = self.store.state['operations'][operation_id]
        if operation['phase'] == 'confirmed':
            return
        fresh = self._find(operation['new']['path'])
        if not fresh or fresh['id'] != operation['new']['id'] or fresh['size'] != operation['new']['size']:
            raise CloudPending('Active cloud version requires reconciliation')
        old = operation.get('old')
        if old:
            observed = self._find(old['path'])
            if observed:
                if observed['id'] != old['id'] or observed['size'] != old['size']:
                    raise CloudPending('Cloud recovery identity changed')
                if operation.get('phase') == 'retire_intent':
                    raise CloudPending('Cloud retirement is awaiting reconciliation')
                self._record(operation, 'retire_intent')
                self.openlist.fs('remove', {'dir':self.mount+str(PurePosixPath(old['path']).parent),
                    'names':[PurePosixPath(old['path']).name]})
                if self._find(old['path']):
                    raise CloudPending('Cloud retirement is not complete')
        self._record(operation, 'confirmed')
