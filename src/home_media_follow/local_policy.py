"""Persistent local opt-outs; cloud inventory and activation remain independent."""
from .core import in_requested_range, logical_key


def local_requested(candidate, subscription):
    return (logical_key(candidate) not in subscription.get('local_suppressions', {})
            and in_requested_range(candidate, subscription['activation']))


def completed_asset_receipt(state, show_id, key, asset):
    """Only a successfully published current version can establish removal."""
    cid = asset.get('candidate', {}).get('candidate_id')
    if not asset.get('managed_local') or not cid or not asset.get('destination'):
        return None
    return next((job for job in state.get('downloads', {}).values()
        if job.get('status') == 'complete' and job.get('show_id') == show_id
        and job.get('logical_key') == key and job.get('candidate', {}).get('candidate_id') == cid
        and job.get('destination') == asset['destination']
        and job.get('jellyfin', {}).get('status') == 'verified'
        and job.get('jellyfin', {}).get('playable') is True), None)
