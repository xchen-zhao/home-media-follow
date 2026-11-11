"""Check distributable contents and privacy patterns. Does not modify files."""
from pathlib import Path
import ast
import hashlib
import json
import re
import sys
import tarfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SKIP = {'.git', '.venv', '.build-venv', '.package-venv', '__pycache__', 'build', 'dist', 'htmlcov'}
REQUIRED = {'LICENSE', 'LICENSE-DOCS', 'THIRD_PARTY_NOTICES.md', 'LICENSES/Apache-2.0.txt'}

def main():
    issues = []
    for path in ROOT.rglob('*'):
        rel = path.relative_to(ROOT)
        if not path.is_file() or any(p in SKIP or p.endswith('.egg-info') or p.startswith('.venv') for p in rel.parts):
            continue
        if path.suffix in {'.dpapi', '.cookie', '.cookies', '.mp4', '.mkv', '.log'}:
            issues.append(str(rel) + ': runtime/private file')
            continue
        try: text = path.read_text(encoding='utf-8')
        except UnicodeError:
            issues.append(str(rel) + ': unexpected binary'); continue
        # Avoid embedding real private identifiers into the public scanner itself.
        patterns = [r'/(?:Users|home)/[A-Za-z0-9_.-]+/', r'100\.(?:6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.\d+\.\d+',
                    r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----']
        if any(re.search(p, text) for p in patterns): issues.append(str(rel) + ': private path/key/address')
        if path.suffix == '.py':
            try: ast.parse(text)
            except SyntaxError: issues.append(str(rel) + ': invalid Python')
    manifest = ROOT / 'docs' / 'verification.json'
    if manifest.exists():
        recorded = json.loads(manifest.read_text(encoding='utf-8'))
        actual = {p.name:hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in (ROOT / 'src' / 'home_media_follow').glob('*.py')}
        bundle = hashlib.sha256(json.dumps(actual, sort_keys=True).encode('utf-8')).hexdigest()
        if recorded.get('modules') != actual or recorded.get('code_bundle_sha256') != bundle:
            issues.append('verification manifest does not match source module digests')
    wheels = list((ROOT / 'dist').glob('*.whl'))
    sources = list((ROOT / 'dist').glob('*.tar.gz'))
    if not wheels or not sources: issues.append('wheel and source archive required')
    for p in wheels:
        with zipfile.ZipFile(p) as z:
            names = z.namelist()
            for n in REQUIRED:
                if not any(x.endswith('/licenses/' + n) for x in names): issues.append(p.name + ': missing ' + n)
    for p in sources:
        with tarfile.open(p) as z:
            names = z.getnames()
            for n in REQUIRED:
                if not any(x.endswith('/' + n) for x in names): issues.append(p.name + ': missing ' + n)
    print(json.dumps({'ok':not issues, 'issues':issues, 'scope':'working tree and built distribution; manually review fixture literals and Git history'}, ensure_ascii=False))
    return bool(issues)

if __name__ == '__main__': sys.exit(main())
