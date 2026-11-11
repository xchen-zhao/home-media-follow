"""Common atomic receipts and publisher lock; hooks never open the follow Store."""
import contextlib
import json
import os
from pathlib import Path
import time
import uuid
import hashlib

MANIFEST_FIELDS=('download_id','gid','show_id','request_id','logical_key','candidate',
                 'stage_file','destination','previous_sha256')


def manifest_identity(job):
    payload={key:job.get(key) for key in MANIFEST_FIELDS}
    return hashlib.sha256(json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def write_manifest(root, job):
    root=Path(root);path=root/'state/manifests'/(job['gid']+'.json')
    atomic_json(path,job)
    atomic_json(root/'state/gids'/(job['gid']+'.json'),{'gid':job['gid'],'download_id':job['download_id'],
        'manifest_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'ownership_sha256':manifest_identity(job)})


def read_manifest(root, gid, job=None):
    root=Path(root);raw=(root/'state/manifests'/(gid+'.json')).read_bytes()
    index=json.loads((root/'state/gids'/(gid+'.json')).read_bytes())
    if index.get('gid')!=gid or index.get('manifest_sha256')!=hashlib.sha256(raw).hexdigest():
        raise ValueError('Managed manifest digest mismatch')
    document=json.loads(raw)
    if document.get('gid')!=gid or index.get('download_id')!=document.get('download_id'):
        raise ValueError('Managed manifest identity mismatch')
    if index.get('ownership_sha256')!=manifest_identity(document) or (job is not None and manifest_identity(job)!=manifest_identity(document)):
        raise ValueError('Managed manifest differs from its durable download intent')
    return document


def atomic_json(path, value):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name('.'+uuid.uuid4().hex+'.pending')
    with temporary.open('xb') as stream:
        stream.write(json.dumps(value,ensure_ascii=False,allow_nan=False).encode())
        stream.flush(); os.fsync(stream.fileno())
    for attempt in range(30):
        try:
            temporary.replace(path); return
        except PermissionError:
            if attempt == 29: raise
            time.sleep(.1)


@contextlib.contextmanager
def publisher_lock(path, timeout=120):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as handle:
        if handle.seek(0,2) == 0:
            handle.write(b'\0'); handle.flush()
        deadline = time.monotonic()+timeout
        while True:
            try:
                handle.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline: raise TimeoutError('Publisher is busy') from None
                time.sleep(.1)
        try: yield
        finally:
            if os.name == 'nt':
                handle.seek(0); msvcrt.locking(handle.fileno(),msvcrt.LK_UNLCK,1)
            else: fcntl.flock(handle.fileno(),fcntl.LOCK_UN)
