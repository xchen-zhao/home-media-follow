"""Windows installer validation must reject unsafe grants before any mutation."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from home_media_follow.cli import initialize


@unittest.skipUnless(os.name == 'nt', 'Windows PowerShell regression')
class WindowsTaskSafetyTests(unittest.TestCase):
    def test_real_junction_is_rejected_by_init_secret_and_task_preflight(self):
        from home_media_follow.cli import initialize
        from home_media_follow.configuration import ConfigurationError
        from home_media_follow.secrets import checked_secret_path, SecretError
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            root = base / 'instance'
            initialize(root)
            outside = base / 'outside'
            outside.mkdir()
            link = root / 'linked'
            result = subprocess.run(['cmd.exe', '/d', '/c', 'mklink', '/J', str(link), str(outside)], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            try:
                with self.assertRaises(ConfigurationError): initialize(link / 'new-instance')
                with self.assertRaises(SecretError): checked_secret_path(link / 'credentials.dpapi')
                config_path = root / 'config/shows.json'
                config = json.loads(config_path.read_text(encoding='utf-8'))
                config['media_root'] = str(link / 'videos')
                config_path.write_text(json.dumps(config), encoding='utf-8')
                installer = Path(__file__).resolve().parents[1] / 'tools/windows-task.ps1'
                result = subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(installer), '-Action', 'install', '-Root', str(root), '-Python', sys.executable, '-TaskName', 'HomeMediaFollow-JunctionRegression', '-DryRun'], capture_output=True, timeout=20)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(b'Reparse paths are not allowed', result.stderr)
                self.assertEqual(list(outside.iterdir()), [])
            finally:
                link.rmdir()

    def test_root_lock_is_rejected_before_mutation_with_or_without_dry_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve() / 'instance'
            initialize(root)
            config_path = root / 'config/shows.json'
            config = json.loads(config_path.read_text(encoding='utf-8'))
            config['publisher_lock'] = str(root / 'publisher.lock')
            config_path.write_text(json.dumps(config), encoding='utf-8')
            installer = Path(__file__).resolve().parents[1] / 'tools/windows-task.ps1'
            def literal(value): return "'" + str(value).replace("'", "''") + "'"
            for dry_run in (False, True):
                script = """
$ErrorActionPreference='Stop'
function Get-ScheduledTask { [CmdletBinding()]param([string]$TaskName) return $null }
function icacls.exe { throw 'MUTATION_REACHED' }
function Register-ScheduledTask { throw 'MUTATION_REACHED' }
""" + '& ' + literal(installer) + ' -Action install -Root ' + literal(root) + ' -Python ' + literal(sys.executable) + ' -TaskName HomeMediaFollow-SafetyRegression' + (' -DryRun' if dry_run else '')
                result = subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-Command',script],capture_output=True,timeout=30)
                combined = (result.stdout + result.stderr).decode('utf-8', errors='replace')
                self.assertNotEqual(result.returncode, 0, combined)
                self.assertIn('Publisher lock requires a dedicated subdirectory', combined)
                self.assertNotIn('MUTATION_REACHED', combined)
                self.assertEqual(json.loads(config_path.read_text(encoding='utf-8'))['publisher_lock'], str(root / 'publisher.lock'))

if __name__ == '__main__': unittest.main()
