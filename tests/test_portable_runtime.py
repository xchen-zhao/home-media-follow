import json
import stat
import tempfile
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
from home_media_follow.worker import Worker
from home_media_follow.secrets import checked_secret_path, SecretError
from home_media_follow.integrations import OpenList, Aria2
from home_media_follow.baidu import BaiduAdapter, BaiduError
from home_media_follow.publisher import write_metadata, variety_display_episode
from home_media_follow.core import logical_key


class PortableRuntimeTests(unittest.TestCase):
    def test_root_local_secrets_services_and_probe_configuration(self):
        with TemporaryDirectory() as directory:
            root=Path(directory);(root/'config').mkdir()
            config={'shows':[], 'cloud_root':'/OwnTests','openlist_mount':'/Library',
                    'services':{'openlist':'http://127.0.0.1:45244','aria2':'http://127.0.0.1:46800','jellyfin':'http://127.0.0.1:48096'},'ffprobe':'own-ffprobe'}
            (root/'config/shows.json').write_text(json.dumps(config))
            secrets=SimpleNamespace(load=lambda:{'openlist':{'username':'test','password':'test'},'aria2':{'secret':'test'},'jellyfin':{'api_key':'test'}},baidu_session=lambda:'test')
            with patch('home_media_follow.worker.SecretStore',return_value=secrets) as store:
                subject=Worker(root)
            self.assertEqual(store.call_args.args[0],root/'secrets/service.dpapi')
            self.assertEqual(subject.openlist.client.base_url,config['services']['openlist'])
            self.assertEqual(subject.aria2.client.base_url,config['services']['aria2'])
            self.assertEqual(subject.jellyfin.client.base_url,config['services']['jellyfin'])
            self.assertEqual(subject.provider.cloud_root,'/OwnTests')
            self.assertEqual(subject.cloud.mount,'/Library')

    def test_static_sources_make_no_catalog_request(self):
        subject=Worker.__new__(Worker);subject.config={}
        with patch('home_media_follow.worker.Catalog') as catalog:
            subject._catalog([])
        catalog.assert_not_called()

    def test_variety_names_preserve_parts_without_private_rules(self):
        subject=Worker.__new__(Worker);subject.config={'media_root':'D:/Example'}
        show={'title':'Example','media_kind':'variety'}
        candidate={'season':4,'episode':5,'part':'下','name':'input.mp4'}
        result=subject._destination(show,candidate)
        self.assertIn('S04E15 - 第5期下',result.name)
        self.assertEqual(subject._publication_label(show,candidate),'第5期下')

    def test_builtin_metadata_is_atomic_and_valid_xml(self):
        with TemporaryDirectory() as directory:
            video=Path(directory)/'Example - S04E15 - 第5期下.mp4';video.write_bytes(b'test')
            write_metadata(video,'第5期下')
            meta=ET.parse(video.with_suffix('.nfo')).getroot()
            self.assertEqual(meta.findtext('title'),'第5期下')
            self.assertEqual(meta.findtext('season'),'04')
            self.assertEqual(meta.findtext('episode'),'15')
            self.assertFalse(list(Path(directory).glob('*.pending')))

    def test_variety_display_slots_never_collide_across_periods_and_parts(self):
        slots=[variety_display_episode(period,part) for period in range(1,101) for part in (None,'上','下')]
        self.assertEqual(len(set(slots)),300)
        self.assertEqual([variety_display_episode(5,p) for p in (None,'上','下')],[13,14,15])
        self.assertEqual(variety_display_episode(6),16)
        for period in (0,-1,True,1.5):
            with self.assertRaises(ValueError):variety_display_episode(period)
        with self.assertRaises(ValueError):variety_display_episode(1,'花絮')

    def test_variety_display_mapping_preserves_original_logical_period_and_part(self):
        subject=Worker.__new__(Worker);subject.config={'media_root':'D:/Example'}
        show={'title':'Example','show_id':'example','media_kind':'variety'}
        expected={None:13,'上':14,'下':15}
        for part,index in expected.items():
            candidate={'show_id':'example','season':4,'episode':5,'part':part,'name':'input.mp4'}
            before=dict(candidate);key=logical_key(candidate)
            path=subject._destination(show,candidate)
            self.assertIn(f'S04E{index:02d}',path.name)
            self.assertEqual(candidate,before)
            self.assertEqual(logical_key(candidate),key)
            self.assertIn(':E0005:',key)
            self.assertIn('第5期'+(part or ''),path.name)

    def test_custom_cloud_boundary_cannot_escape(self):
        provider=BaiduAdapter(lambda:'fixture',cloud_root='/OwnTests')
        with self.assertRaises(BaiduError):
            provider.list_target('/HomeMediaFollow/Other')
        client=OpenList({'username':'test','password':'test'},mount='/Library',cloud_root='/OwnTests')
        with self.assertRaises(ValueError):client.fs('mkdir',{'path':'/Library/Elsewhere/Show'})
        with self.assertRaises(ValueError):client.fs('mkdir',{'path':'/Library/OwnTests'})
        with patch.object(client,'_api',return_value={'ok':True}) as api:
            client.fs('mkdir',{'path':'/Library/OwnTests/Show'})
        api.assert_called_once()


class SecretPathTests(unittest.TestCase):
    def test_secret_path_rejects_symlink_ancestor(self):
        with TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve()) as directory:
            root=Path(directory);(root/'actual').mkdir()
            try: (root/'link').symlink_to(root/'actual',target_is_directory=True)
            except OSError: self.skipTest('Symlink creation unavailable')
            with self.assertRaises(SecretError): checked_secret_path(root/'link'/'secrets')

    def test_secret_path_rejects_junction_attributes_without_following_target(self):
        info=SimpleNamespace(st_mode=stat.S_IFDIR,st_file_attributes=0x400)
        with patch.object(Path,'lstat',return_value=info),patch.object(Path,'stat') as follow:
            with self.assertRaises(SecretError): checked_secret_path('/instance/secrets')
        follow.assert_not_called()
