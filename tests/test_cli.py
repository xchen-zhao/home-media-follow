import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from home_media_follow import cli
from home_media_follow.configuration import validate, ConfigurationError, load


class CLITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve() / 'instance'
        cli.initialize(self.root)
    def tearDown(self): self.tmp.cleanup()
    def command(self, *args):
        result = io.StringIO()
        with contextlib.redirect_stdout(result): code = cli.main(['--root', str(self.root), *args])
        return code, json.loads(result.getvalue())
    def configured(self):
        config = load(self.root)
        config['shows'] = [{'show_id':'example','title':'Example','media_kind':'tv','season':1,'episode_mode':'absolute','cloud_path':'/HomeMediaFollow/Example','source':{'short_url':'fixture-only','source_root_id':'1','source_root':'/Example','extraction_secret_ref':'example'}}]
        (self.root/'config/shows.json').write_text(json.dumps(config))
        return config
    def test_empty_init_and_check_and_status(self):
        self.assertEqual(self.command('check')[0], 0)
        code, result = self.command('status')
        self.assertEqual(code, 0)
        self.assertEqual(result['state_status'], 'missing')
        self.assertFalse((self.root/'state/state.json').exists())
    def test_init_refuses_adoption(self):
        with self.assertRaises(ConfigurationError): cli.initialize(self.root)
    def test_machine_json_is_ascii_independent_of_windows_code_page(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output): cli._json({'title': '测试作品'})
        self.assertTrue(output.getvalue().isascii())
        self.assertEqual(json.loads(output.getvalue())['title'], '测试作品')

    def test_config_rejects_unsupported_scan_and_reserved_titles(self):
        config = self.configured()
        for interval in [1, 899, 86401]:
            changed = dict(config, scan_interval_seconds=interval)
            with self.assertRaises(ConfigurationError): validate(changed)
        for title in ['CON', 'nul.mp4', 'LPT1', 'Bad/Title', 'trailing.']:
            config['shows'][0]['title'] = title
            with self.assertRaises(ConfigurationError): validate(config)

    def test_config_rejects_unimplemented_timezone_and_embedded_share_secrets(self):
        config = self.configured()
        with self.assertRaises(ConfigurationError): validate(dict(config, scan_timezone='UTC'))
        for reference in ['https://pan.baidu.com/s/1fixture?pwd=abcd', 'fixture?token=private']:
            config['shows'][0]['source']['short_url'] = reference
            with self.assertRaises(ConfigurationError): validate(config)

    def test_init_rejects_reparse_ancestor(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        import stat
        with patch.object(Path, 'lstat', return_value=SimpleNamespace(st_file_attributes=0x400, st_mode=stat.S_IFDIR)):
            with self.assertRaises(ConfigurationError): cli.initialize(self.root)

    def test_prepare_and_submit_retry_uses_same_identity(self):
        self.configured()
        request = Path(self.tmp.name)/'request.json'
        self.assertEqual(self.command('prepare','--action','activate_latest','--show-id','example','--request-file',str(request))[0], 0)
        original = request.read_bytes()
        self.assertEqual(self.command('submit','--request-file',str(request))[1]['duplicate'], False)
        self.assertEqual(self.command('submit','--request-file',str(request))[1]['duplicate'], True)
        self.assertEqual(request.read_bytes(), original)
        self.assertNotEqual(self.command('prepare','--action','activate_latest','--show-id','example','--request-file',str(request))[0], 0)
        self.assertEqual(request.read_bytes(), original)
    def test_config_rejects_secrets_and_remote_endpoints(self):
        for field, value in [('password','fixture'),('services',{'openlist':'https://example.com','aria2':'http://127.0.0.1:6800','jellyfin':'http://127.0.0.1:8096'})]:
            config = load(self.root);config[field] = value
            with self.assertRaises(ConfigurationError): validate(config)
    def test_inspect_source_writes_binding_not_secret_and_does_not_activate(self):
        from unittest.mock import patch, Mock
        from types import SimpleNamespace
        destination = Path(self.tmp.name) / 'proposal.json'
        provider = Mock()
        provider._short.return_value = 'fixture-reference'
        provider.list_share.return_value = [{'id':'123','name':'Example','path':'/Example','is_dir':True}]
        store = Mock()
        store.load.return_value = {}
        with patch.object(cli, 'os', SimpleNamespace(name='nt', open=cli.os.open, fdopen=cli.os.fdopen, O_WRONLY=cli.os.O_WRONLY, O_CREAT=cli.os.O_CREAT, O_EXCL=cli.os.O_EXCL)), patch.object(cli.sys.stdin, 'isatty', return_value=True), patch.object(cli.getpass, 'getpass', side_effect=['fixture-reference','abcd']), patch('home_media_follow.baidu.BaiduAdapter',return_value=provider), patch('home_media_follow.secrets.SecretStore',return_value=store):
            code, result = self.command('inspect-source','--show-id','example','--output',str(destination))
        self.assertEqual(code, 0)
        self.assertFalse(result['subscriptions_activated'])
        self.assertNotIn('abcd', destination.read_text())
        self.assertNotIn('fixture-reference', json.dumps(result))
        proposal = json.loads(destination.read_text())
        self.assertEqual(proposal['bindings'][0]['source']['source_root_id'], '123')
        store.update.assert_called_once_with({'extraction_codes':{'example':'abcd'}})
        self.assertFalse(any((self.root/'requests').iterdir()))

    def test_baidu_authorization_uses_protected_dedicated_visible_browser(self):
        from unittest.mock import patch, Mock
        from types import SimpleNamespace
        store = Mock()
        def launch(sink, **kwargs):
            self.assertFalse(kwargs['headless'])
            self.assertEqual(kwargs['profile_dir'], self.root / 'login/profile')
            sink({'cookie': 'fixture-cookie'})
            return {'status':'session_saved'}
        with patch.object(cli, 'os', SimpleNamespace(name='nt')), patch.object(cli.sys.stdin, 'isatty', return_value=True), patch('home_media_follow.secrets.SecretStore',return_value=store), patch('home_media_follow.secrets.protect_secret_directory') as protect, patch('home_media_follow.baidu_auth.launch_edge_login',side_effect=launch):
            code, result = self.command('authorize','baidu')
        self.assertEqual(code, 0)
        protect.assert_called_once_with(self.root / 'login')
        store.update.assert_called_once_with({'baidu_session': {'cookie': 'fixture-cookie'}})
        self.assertNotIn('fixture-cookie', json.dumps(result))
        self.assertTrue(result['requires_capability_validation'])

    def test_authorize_nonlocal_does_not_prompt_or_write(self):
        code, result = self.command('authorize','baidu')
        self.assertNotEqual(code, 0)
        self.assertEqual(result['error_code'], 'authorization_requires_local_windows_terminal')
        self.assertFalse((self.root/'secrets/service.dpapi').exists())

if __name__ == '__main__': unittest.main()
