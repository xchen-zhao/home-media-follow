"""Self-contained episode metadata writer. No private rules or external artwork."""
from pathlib import Path
import os
import re
import uuid
import xml.etree.ElementTree as ET


def variety_display_episode(period, part=None):
    """Allocate independent Jellyfin episode slots without changing logical keys.

    Each original period owns three slots: unsplit, upper and lower. Reserving
    all slots makes later parts stable and avoids Jellyfin merging file versions.
    """
    if type(period) is not int or period < 1:
        raise ValueError('Variety period must be a positive integer')
    offsets={None:-2,'':-2,'上':-1,'下':0}
    if part not in offsets: raise ValueError('Unsupported variety part')
    return 3*period+offsets[part]


def write_metadata(destination, label):
    """Write an episode NFO atomically beside an already published video."""
    destination=Path(destination)
    if not destination.is_file(): raise ValueError('Published video missing')
    match=re.search(r' - S(\d+)E(\d+)',destination.stem)
    if not match: raise ValueError('Expected canonical season/episode filename')
    root=ET.Element('episodedetails')
    for name,value in [('title',str(label)),('season',match[1]),('episode',match[2])]:
        ET.SubElement(root,name).text=value
    data=ET.tostring(root,encoding='utf-8',xml_declaration=True)
    target=destination.with_suffix('.nfo')
    temporary=target.with_name('.'+uuid.uuid4().hex+'.pending')
    try:
        with temporary.open('xb') as stream:
            stream.write(data);stream.flush();os.fsync(stream.fileno())
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def create_poster(destination):
    """Artwork is optional; this package does not fetch copyrighted posters."""
    return None
