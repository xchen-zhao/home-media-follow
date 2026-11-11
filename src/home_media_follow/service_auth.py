"""User-assisted Jellyfin authorization; only the dedicated API key is saved."""
from __future__ import annotations

import urllib.parse

from .integrations import IntegrationError, _Client


APP_NAME = "HomeMedia-Follow"
CLIENT_AUTH = 'MediaBrowser Client="HomeMedia-Follow", Device="Local Windows", DeviceId="home-media-follow-local-authorization", Version="0.1.0"'


class ServiceAuthError(RuntimeError):
    def __init__(self, operation: str, status_code: int | None = None):
        self.operation, self.status_code = operation, status_code
        suffix = f" (HTTP {status_code})" if status_code is not None else ""
        super().__init__("Jellyfin authorization " + operation + " failed" + suffix)


def _authorization(token: str | None = None) -> dict:
    value = CLIENT_AUTH
    if token is not None:
        if not isinstance(token, str) or not token or any(ord(char) < 32 or ord(char) == 127 for char in token):
            raise ServiceAuthError("session")
        value += ', Token="' + token.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return {"Authorization": value}


def authorize_jellyfin(username: str, login_secret: str, secret_sink, base_url: str = "http://127.0.0.1:8096", *, timeout: float = 15) -> dict:
    """Authenticate locally, verify admin, reuse/create a key, then encrypt via sink.

    ``secret_sink`` receives only ``{'jellyfin': {'api_key': key}}``. It should be
    SecretStore.update on Windows, never a plaintext writer. No token is returned.
    """
    if not isinstance(username, str) or not username.strip() or not isinstance(login_secret, str) or not login_secret or not callable(secret_sink):
        raise ServiceAuthError("input")
    try:
        client = _Client("Jellyfin", base_url, timeout)
    except Exception:
        raise ServiceAuthError("endpoint") from None

    def request(operation, path, *, method="GET", body=None, headers=None):
        try:
            return client.request(operation, path, method=method, body=body, headers=headers)
        except IntegrationError as error:
            raise ServiceAuthError(operation, error.status_code) from None
        except Exception:
            raise ServiceAuthError(operation) from None

    session_token = None
    key = None
    try:
        auth = request("login", "/Users/AuthenticateByName", method="POST", body={"Username": username, "Pw": login_secret}, headers=_authorization())
        login_secret = None
        if not isinstance(auth, dict) or not isinstance(auth.get("User"), dict) or not auth.get("AccessToken"):
            raise ServiceAuthError("login_result")
        user, session_token = auth["User"], auth["AccessToken"]
        headers = _authorization(session_token)
        policy = user.get("Policy")
        if not isinstance(policy, dict):
            user_id = user.get("Id")
            if not isinstance(user_id, str) or not user_id:
                raise ServiceAuthError("administrator_required")
            details = request("user_policy", "/Users/" + urllib.parse.quote(user_id, safe=""), headers=headers)
            policy = details.get("Policy") if isinstance(details, dict) else None
        if not isinstance(policy, dict) or policy.get("IsAdministrator") is not True:
            raise ServiceAuthError("administrator_required")

        def dedicated_keys():
            result = request("list_keys", "/Auth/Keys", headers=headers)
            entries = result.get("Items") if isinstance(result, dict) else result
            if not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
                raise ServiceAuthError("key_list_result")
            total = result.get("TotalRecordCount") if isinstance(result, dict) else None
            if isinstance(total, int) and total != len(entries):
                # Never create a duplicate because a partial listing hid an old key.
                raise ServiceAuthError("key_list_incomplete")
            return sorted((entry for entry in entries if entry.get("AppName") == APP_NAME), key=lambda entry: (str(entry.get("DateCreated", "")), str(entry.get("Id", "")), str(entry.get("AccessToken", ""))))

        entries = dedicated_keys()
        action = "reused"
        if not entries:
            action = "created"
            create_error = None
            try:
                request("create_key", "/Auth/Keys?" + urllib.parse.urlencode({"app": APP_NAME}), method="POST", headers=headers)
            except ServiceAuthError as error:
                create_error = error
            # A lost creation response is reconciled by listing, never by POST retry.
            try:
                entries = dedicated_keys()
            except ServiceAuthError:
                if create_error:
                    raise create_error from None
                raise
            if not entries:
                if create_error:
                    raise create_error from None
                raise ServiceAuthError("created_key_missing")

        for entry in entries:
            candidate = entry.get("AccessToken")
            if not isinstance(candidate, str) or not candidate:
                continue
            try:
                info = request("verify_key", "/System/Info", headers=_authorization(candidate))
            except ServiceAuthError as error:
                if error.status_code in {401, 403}:
                    continue
                raise
            if not isinstance(info, dict) or not info.get("Id") or not info.get("Version"):
                raise ServiceAuthError("verify_key_result")
            key = candidate
            break
        if key is None:
            raise ServiceAuthError("dedicated_key_unavailable")
        try:
            secret_sink({"jellyfin": {"api_key": key}})
        except Exception:
            raise ServiceAuthError("credential_store") from None
        return {"status": "authorized", "service": "jellyfin", "key_action": action}
    finally:
        if session_token:
            try:
                request("logout", "/Sessions/Logout", method="POST", headers=_authorization(session_token))
            except Exception:
                pass
        # Drop references; Python strings cannot promise secure in-place zeroization.
        login_secret = None
        session_token = None
        key = None
