"""Optional managed completion gate. Unregistered downloads fail closed."""
import json
from pathlib import Path
import subprocess
import sys
import ntpath
from .core import resolution_rank
from .probe import probe_file
from .runtime import atomic_json, read_manifest


def gate(manifest, source, prober=probe_file):
    source = Path(source)
    if source.resolve() != Path(manifest['stage_file']).resolve() or not source.is_file():
        return {'status':'rejected','reason':'staging_identity_mismatch'}
    if source.stat().st_size != manifest['candidate']['size']:
        return {'status':'rejected','reason':'staging_size_mismatch'}
    result = prober(source)
    expected = resolution_rank(manifest['candidate'])
    observed = resolution_rank({'probe':result})
    before = manifest['candidate']['probe']
    if not expected or not observed or observed[0] < expected[0] or (result.get('width'), result.get('height')) != (before.get('width'),before.get('height')):
        return {'status':'rejected','reason':'media_parameters_changed','probe':result}
    if before.get('duration') and abs(result['duration']-before['duration']) > max(2, before['duration']*.01):
        return {'status':'rejected','reason':'media_duration_changed','probe':result}
    return {'status':'verified','probe':result,'bytes':source.stat().st_size}


def main(argv=None, root='./instance'):
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 3: return 2
    gid, _, source = args[:3]; root = Path(root)
    if not __import__('re').fullmatch('[a-fA-F0-9]{16}',gid): return 2
    manifest_path = root/'state/manifests'/(gid+'.json')
    if manifest_path.exists():
        try:
            manifest = read_manifest(root,gid)
            if manifest.get('gid') != gid: raise ValueError('GID mismatch')
            result = gate(manifest,source)
        except Exception as error:
            result = {'status':'rejected','reason':type(error).__name__}
        atomic_json(root/'state/hook-receipts'/(gid+'.json'),{'gid':gid,**result})
        return 0 if result['status']=='verified' else 1
    # An unregistered callback has no authorization. Never invoke a private
    # legacy publisher or process another installation's download.
    return 1
