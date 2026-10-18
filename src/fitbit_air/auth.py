"""Google OAuth login (browser + localhost redirect), Keychain storage, and access-token supply."""
from __future__ import annotations

import json
import os
from typing import Callable

import keyring
import keyring.errors
from google.auth.exceptions import RefreshError, TransportError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

from .config import KEYRING_SERVICE, KEYRING_USER, OAUTH_PORT, SCOPES, Paths
from .errors import ApiError, ReauthRequired


def save_credentials(creds: Credentials) -> None:
    # Access tokens live in memory only; the Keychain holds just the long-lived refresh token.
    keyring.set_password(KEYRING_SERVICE, KEYRING_USER, creds.to_json(strip=["token", "expiry"]))


def load_credentials() -> Credentials | None:
    try:
        raw = keyring.get_password(KEYRING_SERVICE, KEYRING_USER)
        # Use the scopes saved with the login itself, not the currently requested ones.
        return Credentials.from_authorized_user_info(json.loads(raw)) if raw else None
    except keyring.errors.KeyringError as exc:
        # A locked or denied Keychain is transient: this run's type fails and the next run retries.
        raise ApiError(0, f"cannot read Keychain: {exc}") from exc
    except ValueError as exc:
        raise ReauthRequired(f"cannot read stored login: {exc}") from exc


def auth_state() -> str:
    try:
        return "logged in" if load_credentials() is not None else "not logged in"
    except ReauthRequired as exc:
        return f"unknown ({str(exc).removeprefix('cannot read stored login: ')})"
    except ApiError as exc:
        return f"unknown ({exc.message.removeprefix('cannot read Keychain: ')})"


def missing_scopes(creds: Credentials) -> list[str]:
    """Requested scopes that Google did not grant (the user may untick some on the consent screen)."""
    return sorted(set(SCOPES) - set(creds.granted_scopes or creds.scopes or ()))


def login(paths: Paths) -> Credentials:
    """Open the browser for Google consent; store the resulting refresh token in the Keychain."""
    if not paths.client_secret.exists():
        raise FileNotFoundError(f"OAuth client JSON not found at {paths.client_secret} — see docs/setup.md, step 4.")
    from google_auth_oauthlib.flow import InstalledAppFlow

    # Allow partial grants (the user may untick scopes) instead of raising; missing_scopes() reports them.
    os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
    flow = InstalledAppFlow.from_client_secrets_file(str(paths.client_secret), scopes=SCOPES)
    creds = flow.run_local_server(port=OAUTH_PORT, access_type="offline", prompt="consent", open_browser=True)
    if creds.refresh_token is None:
        raise RuntimeError(
            "Google returned no refresh token — revoke the app at "
            "https://myaccount.google.com/permissions and run `fitbit-air auth` again"
        )
    granted = list(creds.granted_scopes or ())
    if granted and set(granted) != set(creds.scopes or ()):
        # Persist only what the user granted, so later refreshes don't request scopes they declined.
        save_credentials(Credentials(token=None, refresh_token=creds.refresh_token, token_uri=creds.token_uri,
                                     client_id=creds.client_id, client_secret=creds.client_secret, scopes=granted))
    else:
        save_credentials(creds)
    return creds


class TokenProvider:
    """Callable returning a valid access token; refreshes as needed. Raises ReauthRequired if it can't."""

    def __init__(self, loader: Callable[[], Credentials | None] = load_credentials,
                 request_factory: Callable[[], object] = Request,
                 saver: Callable[[Credentials], None] = save_credentials):
        self._loader = loader
        self._saver = saver
        self._request_factory = request_factory
        self._creds = None

    def __call__(self, force_refresh: bool = False) -> str:
        if self._creds is None:
            self._creds = self._loader()
            if self._creds is None:
                raise ReauthRequired("not logged in — run `fitbit-air auth`")
        if force_refresh or not self._creds.valid:
            before = self._creds.refresh_token
            try:
                self._creds.refresh(self._request_factory())
            except RefreshError as exc:
                if getattr(exc, "retryable", False):
                    raise ApiError(0, f"token refresh temporarily failed: {exc}") from exc
                raise ReauthRequired(str(exc)) from exc
            except TransportError as exc:
                raise ApiError(0, f"token refresh network error: {exc}") from exc
            if self._creds.refresh_token != before:
                self._saver(self._creds)
        return self._creds.token
