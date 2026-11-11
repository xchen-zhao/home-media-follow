"""Windows machine DPAPI envelopes; filesystem ACLs restrict the service secret."""
import ctypes
import json
import os
import time
import uuid
import contextlib
import base64
import subprocess
import stat
from pathlib import Path


class SecretError(RuntimeError):
    pass


class _Blob(ctypes.Structure):
    _fields_ = [("size", ctypes.c_uint32), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def _crypt(data, decrypt=False):
    if os.name != "nt":
        raise SecretError("DPAPI requires a Windows host")
    buffer = ctypes.create_string_buffer(data)
    source = _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = _Blob()
    crypt32 = ctypes.windll.crypt32
    crypt32.CryptProtectData.argtypes = [ctypes.POINTER(_Blob), ctypes.c_wchar_p,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(_Blob)]
    crypt32.CryptUnprotectData.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(_Blob)]
    ctypes.windll.kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    ctypes.windll.kernel32.LocalFree.restype = ctypes.c_void_p
    if decrypt:
        ok = crypt32.CryptUnprotectData(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target))
    else:
        ok = crypt32.CryptProtectData(ctypes.byref(source), "HomeMedia Follow", None, None, None, 5, ctypes.byref(target))
    if not ok:
        raise SecretError("DPAPI operation failed")
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        ctypes.windll.kernel32.LocalFree(target.data)


def checked_secret_path(value):
    """Inspect links themselves, including Windows junctions and broken links."""
    path=Path(value).absolute()
    for ancestor in (path,*path.parents):
        try: info=ancestor.lstat()
        except FileNotFoundError: continue
        if stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0) & 0x400:
            raise SecretError('Reparse points are not allowed in secret paths')
    return path


def protect_secret_directory(path):
    """Constrain machine-DPAPI files to their owner, admins, SYSTEM and service."""
    if os.name != 'nt': raise SecretError('Secret storage requires Windows')
    path=checked_secret_path(path)
    for child in path.iterdir(): checked_secret_path(child)
    quoted=str(path).replace("'","''")
    script="""$ErrorActionPreference='Stop'
$dir='REPLACE_PATH'
$current=[Security.Principal.WindowsIdentity]::GetCurrent().User
function Restrict-Path($p,$directory) {
 $acl=Get-Acl -LiteralPath $p
 $owner=if($acl.Owner -like 'S-1-*'){$acl.Owner}else{([Security.Principal.NTAccount]::new($acl.Owner)).Translate([Security.Principal.SecurityIdentifier]).Value}
 $allowed=@($owner,'S-1-5-18','S-1-5-32-544','S-1-5-19')
 $safe=(!$directory -or $acl.AreAccessRulesProtected)
 foreach($r in $acl.Access) {
  $sid=$r.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
  if($r.AccessControlType -eq 'Allow' -and $sid -notin $allowed){$safe=$false}
 }
 if($safe){return}
 if($directory){$new=[Security.AccessControl.DirectorySecurity]::new();$inherit='ContainerInherit,ObjectInherit'}
 else{$new=[Security.AccessControl.FileSecurity]::new();$inherit='None'}
 $new.SetAccessRuleProtection($true,$false)
 $new.SetOwner($current)
 foreach($sid in @($current.Value,'S-1-5-18','S-1-5-32-544','S-1-5-19') | Select-Object -Unique) {
  $rights=if($sid -eq 'S-1-5-19'){'Modify'}else{'FullControl'}
  $rule=[Security.AccessControl.FileSystemAccessRule]::new([Security.Principal.SecurityIdentifier]::new($sid),$rights,$inherit,'None','Allow')
  $new.AddAccessRule($rule)
 }
 Set-Acl -LiteralPath $p -AclObject $new
}
Restrict-Path $dir $true
Get-ChildItem -LiteralPath $dir -Force -File | ForEach-Object {Restrict-Path $_.FullName $false}
""".replace('REPLACE_PATH',quoted)
    encoded=base64.b64encode(script.encode('utf-16le')).decode('ascii')
    try:
        result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-EncodedCommand',encoded],capture_output=True,timeout=30)
    except (OSError,subprocess.TimeoutExpired):
        raise SecretError('Could not protect secret directory ACL') from None
    if result.returncode: raise SecretError('Could not protect secret directory ACL')


class SecretStore:
    def __init__(self, path="./instance/secrets/service.dpapi"):
        self.path = Path(path)

    def load(self):
        checked_secret_path(self.path)
        if not self.path.exists():
            return {}
        try:
            return json.loads(_crypt(self.path.read_bytes(), decrypt=True))
        except Exception as exc:
            raise SecretError("Service credentials unavailable") from exc

    def update(self, changes):
        checked_secret_path(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        protect_secret_directory(self.path.parent)
        with self._locked():
            self._update(changes)

    @contextlib.contextmanager
    def _locked(self):
        import msvcrt
        checked_secret_path(self.path.with_suffix('.lock'))
        with self.path.with_suffix('.lock').open('a+b') as handle:
            if handle.seek(0, 2) == 0:
                handle.write(b'\0'); handle.flush()
            deadline = time.monotonic() + 30
            while True:
                try:
                    handle.seek(0); msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise SecretError('Service secret writer is busy') from None
                    time.sleep(.05)
            try:
                yield
            finally:
                handle.seek(0); msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

    def _update(self, changes):
        values = self.load()
        values.update(changes)
        encrypted = _crypt(json.dumps(values).encode())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name("." + uuid.uuid4().hex + ".pending")
        with temporary.open("xb") as stream:
            stream.write(encrypted)
            stream.flush()
            os.fsync(stream.fileno())
        for attempt in range(20):
            try:
                temporary.replace(self.path)
                return
            except PermissionError:
                if attempt == 19:
                    raise SecretError("Service secret is temporarily locked") from None
                time.sleep(0.1)

    def baidu_session(self):
        values = self.load()
        session = values.get("baidu_session")
        if not session:
            raise SecretError("Independent Baidu login required on this Windows host")
        return session
