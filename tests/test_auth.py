import pytest
import keyring.errors
from google.auth.exceptions import RefreshError, TransportError
from google.oauth2.credentials import Credentials

from fitbit_air import auth
from fitbit_air.config import SCOPES, Paths
from fitbit_air.errors import ApiError, ReauthRequired


class FakeCreds:
    def __init__(self, valid=True, fail=False):
        self.valid = valid
        self.fail = fail
        self.token = "old"
        self.refresh_token = "r-old"
        self.rotate = False
        self.refreshes = 0

    def refresh(self, request):
        if self.fail:
            raise RefreshError("invalid_grant: Token has been expired or revoked.")
        self.refreshes += 1
        self.token = "new"
        self.valid = True
        if self.rotate:
            self.refresh_token = "r-new"


def provider(creds):
    return auth.TokenProvider(loader=lambda: creds, request_factory=object, saver=lambda c: None)


def test_valid_token_returned_without_refresh():
    c = FakeCreds()
    assert provider(c)() == "old" and c.refreshes == 0


def test_invalid_token_is_refreshed():
    assert provider(FakeCreds(valid=False))() == "new"


def test_force_refresh():
    assert provider(FakeCreds())(True) == "new"


def test_refresh_failure_raises_reauth():
    with pytest.raises(ReauthRequired, match="invalid_grant"):
        provider(FakeCreds(valid=False, fail=True))()


def test_not_logged_in_raises_reauth():
    with pytest.raises(ReauthRequired, match="fitbit-air auth"):
        auth.TokenProvider(loader=lambda: None)()


@pytest.fixture
def fake_keyring(monkeypatch):
    vault = {}
    monkeypatch.setattr(auth.keyring, "set_password", lambda s, u, p: vault.__setitem__((s, u), p))
    monkeypatch.setattr(auth.keyring, "get_password", lambda s, u: vault.get((s, u)))
    return vault


def test_credentials_roundtrip_through_keychain(fake_keyring):
    creds = Credentials(token=None, refresh_token="r1", client_id="cid", client_secret="sec",
                        token_uri="https://oauth2.googleapis.com/token", scopes=SCOPES)
    auth.save_credentials(creds)
    loaded = auth.load_credentials()
    assert loaded.refresh_token == "r1" and loaded.client_id == "cid"
    assert auth.auth_state() == "logged in"


def test_auth_state_when_nothing_stored(fake_keyring):
    assert auth.auth_state() == "not logged in"


def test_login_requires_client_secret_file(tmp_path):
    with pytest.raises(FileNotFoundError, match=r"docs/setup\.md, step 4"):
        auth.login(Paths(tmp_path))


def test_retryable_refresh_error_is_api_error_not_reauth():
    class Flaky(FakeCreds):
        def refresh(self, request):
            raise RefreshError("server_error", retryable=True)

    with pytest.raises(ApiError, match="temporarily failed"):
        provider(Flaky(valid=False))()


def test_transport_error_is_api_error():
    class Offline(FakeCreds):
        def refresh(self, request):
            raise TransportError("connection reset")

    with pytest.raises(ApiError, match="network error"):
        provider(Offline(valid=False))()


def test_rotated_refresh_token_is_persisted():
    saved = []
    c = FakeCreds(valid=False)
    c.rotate = True
    auth.TokenProvider(loader=lambda: c, request_factory=object, saver=saved.append)()
    assert saved == [c] and c.refresh_token == "r-new"


def test_unchanged_refresh_token_is_not_saved():
    saved = []
    auth.TokenProvider(loader=lambda: FakeCreds(valid=False), request_factory=object, saver=saved.append)()
    assert saved == []


def test_saved_json_omits_access_token_and_keeps_granted_scopes(fake_keyring):
    import json

    creds = Credentials(token="secret-access", refresh_token="r1", client_id="cid", client_secret="sec",
                        token_uri="https://oauth2.googleapis.com/token", scopes=["only-one-scope"])
    auth.save_credentials(creds)
    (raw,) = fake_keyring.values()
    info = json.loads(raw)
    assert "token" not in info and "expiry" not in info
    assert auth.load_credentials().scopes == ["only-one-scope"]


def test_corrupt_keychain_json_raises_reauth_and_state_is_unknown(fake_keyring):
    fake_keyring[("fitbit-air-mcp", "google-oauth")] = "{not json"
    with pytest.raises(ReauthRequired, match="cannot read stored login"):
        auth.load_credentials()
    assert auth.auth_state().startswith("unknown (")


def test_keyring_error_raises_retryable_api_error_and_state_is_unknown(monkeypatch):
    def boom(s, u):
        raise keyring.errors.KeyringError("locked")

    monkeypatch.setattr(auth.keyring, "get_password", boom)
    with pytest.raises(ApiError, match="cannot read Keychain: locked"):
        auth.load_credentials()
    assert auth.auth_state() == "unknown (locked)"


def test_missing_scopes_reports_ungranted():
    creds = Credentials(token=None, refresh_token="r", scopes=list(SCOPES)[:4])
    assert auth.missing_scopes(creds) == sorted(SCOPES[4:])
    full = Credentials(token=None, refresh_token="r", scopes=list(SCOPES))
    assert auth.missing_scopes(full) == []


def _fake_flow(monkeypatch, creds):
    import google_auth_oauthlib.flow as flow_mod

    class FakeFlow:
        def run_local_server(self, **kw):
            return creds

    monkeypatch.setattr(flow_mod.InstalledAppFlow, "from_client_secrets_file",
                        classmethod(lambda cls, path, scopes: FakeFlow()))


def test_login_without_refresh_token_raises(tmp_path, monkeypatch, fake_keyring):
    monkeypatch.setenv("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
    paths = Paths(tmp_path)
    paths.client_secret.parent.mkdir(parents=True)
    paths.client_secret.write_text("{}")
    _fake_flow(monkeypatch, Credentials(token="t", refresh_token=None, scopes=list(SCOPES)))
    with pytest.raises(RuntimeError, match="no refresh token"):
        auth.login(paths)
    assert fake_keyring == {}


def test_login_saves_and_returns_creds(tmp_path, monkeypatch, fake_keyring):
    monkeypatch.setenv("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
    paths = Paths(tmp_path)
    paths.client_secret.parent.mkdir(parents=True)
    paths.client_secret.write_text("{}")
    creds = Credentials(token="t", refresh_token="r1", client_id="c", client_secret="s",
                        token_uri="https://oauth2.googleapis.com/token", scopes=list(SCOPES))
    _fake_flow(monkeypatch, creds)
    assert auth.login(paths) is creds
    assert len(fake_keyring) == 1


def test_login_saves_only_granted_scopes(tmp_path, monkeypatch, fake_keyring):
    import json

    monkeypatch.setenv("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
    paths = Paths(tmp_path)
    paths.client_secret.parent.mkdir(parents=True)
    paths.client_secret.write_text("{}")
    creds = Credentials(token="t", refresh_token="r1", client_id="c", client_secret="s",
                        token_uri="https://oauth2.googleapis.com/token", scopes=list(SCOPES),
                        granted_scopes=list(SCOPES)[:2])
    _fake_flow(monkeypatch, creds)
    assert auth.login(paths) is creds
    (raw,) = fake_keyring.values()
    assert json.loads(raw)["scopes"] == list(SCOPES)[:2]
    assert "token" not in json.loads(raw)
    assert auth.load_credentials().refresh_token == "r1"
