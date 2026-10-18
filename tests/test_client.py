from datetime import datetime

import httpx
import pytest
import respx

from fitbit_air.client import HealthClient
from fitbit_air.errors import ApiError
from fitbit_air.registry import BY_NAME
from fitbit_air.timeutil import UTC

HOST = "health.googleapis.com"
START = datetime(2026, 10, 1, tzinfo=UTC)
END = datetime(2026, 10, 8, tzinfo=UTC)


class Tokens:
    def __init__(self):
        self.calls = []

    def __call__(self, force_refresh=False):
        self.calls.append(force_refresh)
        return "fresh" if force_refresh else "tok"


def make(tokens=None, sleeps=None, **kw):
    sleeps = [] if sleeps is None else sleeps
    return HealthClient(tokens or Tokens(), http=httpx.Client(), sleep=sleeps.append, rps=0, **kw)


def route(path):
    return respx.route(method="GET", host=HOST, path=f"/v4/users/me/{path}")


@respx.mock
def test_iter_points_paginates_and_sends_filter():
    r = route("dataTypes/steps/dataPoints:reconcile").mock(side_effect=[
        httpx.Response(200, json={"dataPoints": [{"a": 1}], "nextPageToken": "p2"}),
        httpx.Response(200, json={"dataPoints": [{"a": 2}]}),
    ])
    assert list(make().iter_points(BY_NAME["steps"], START, END)) == [[{"a": 1}], [{"a": 2}]]
    first, second = (c.request for c in r.calls)
    assert first.url.params["filter"] == BY_NAME["steps"].filter_for(START, END)
    assert first.url.params["pageSize"] == "10000"
    assert "pageToken" not in first.url.params
    assert second.url.params["pageToken"] == "p2"
    assert first.headers["Authorization"] == "Bearer tok"


@respx.mock
def test_list_method_uses_plain_collection_path():
    r = route("dataTypes/irregular-rhythm-notification/dataPoints").mock(return_value=httpx.Response(200, json={}))
    assert list(make().iter_points(BY_NAME["irregular-rhythm-notification"], START, END)) == [[]]
    assert r.called


@respx.mock
def test_429_honours_retry_after():
    r = route("profile").mock(side_effect=[
        httpx.Response(429, headers={"Retry-After": "7"}),
        httpx.Response(200, json={"age": 35}),
    ])
    sleeps = []
    assert make(sleeps=sleeps).get_profile() == {"age": 35}
    assert sleeps == [7.0] and r.call_count == 2


@respx.mock
def test_5xx_gives_up_after_max_attempts():
    route("profile").mock(return_value=httpx.Response(503, json={"error": {"message": "busy"}}))
    sleeps = []
    with pytest.raises(ApiError) as exc:
        make(sleeps=sleeps, max_attempts=3).get_profile()
    assert exc.value.status == 503 and "busy" in exc.value.message
    assert len(sleeps) == 2


@respx.mock
def test_401_refreshes_token_once():
    r = route("profile").mock(side_effect=[httpx.Response(401), httpx.Response(200, json={"age": 35})])
    tokens = Tokens()
    make(tokens=tokens).get_profile()
    assert tokens.calls == [False, True]
    assert r.calls[1].request.headers["Authorization"] == "Bearer fresh"


@respx.mock
def test_403_is_not_retried():
    r = route("profile").mock(return_value=httpx.Response(403, json={"error": {"message": "scope"}}))
    with pytest.raises(ApiError) as exc:
        make().get_profile()
    assert exc.value.status == 403 and r.call_count == 1


@respx.mock
def test_network_error_is_retried():
    route("profile").mock(side_effect=[httpx.ConnectError("boom"), httpx.Response(200, json={})])
    sleeps = []
    assert make(sleeps=sleeps).get_profile() == {}
    assert len(sleeps) == 1


@respx.mock
def test_paired_devices_path():
    route("pairedDevices").mock(return_value=httpx.Response(200, json={"pairedDevices": []}))
    assert make().list_paired_devices() == {"pairedDevices": []}


@respx.mock
def test_throttle_spaces_requests():
    route("profile").mock(return_value=httpx.Response(200, json={}))
    sleeps = []
    client = HealthClient(Tokens(), http=httpx.Client(), sleep=sleeps.append, clock=lambda: 100.0, rps=2.0)
    client.get_profile()
    client.get_profile()
    assert sleeps == [0.5]


# ---- hardening ---------------------------------------------------------------

from email.utils import format_datetime  # noqa: E402

from fitbit_air.errors import ReauthRequired  # noqa: E402


def _retry_after_sleeps(value, **kw):
    route("profile").mock(side_effect=[
        httpx.Response(429, headers={"Retry-After": value}),
        httpx.Response(200, json={}),
    ])
    sleeps = []
    make(sleeps=sleeps, **kw).get_profile()
    return sleeps


@respx.mock
def test_retry_after_is_clamped_to_maximum():
    assert _retry_after_sleeps("1e9") == [120.0]


@respx.mock
def test_negative_retry_after_clamps_to_zero():
    assert _retry_after_sleeps("-5") == [0.0]


@respx.mock
@pytest.mark.parametrize("value", ["nan", "inf", "garbage"])
def test_non_finite_or_garbage_retry_after_falls_back_to_backoff(value):
    (slept,) = _retry_after_sleeps(value)
    assert 1.0 <= slept <= 2.0  # 2**0 + jitter in [0, 1]


@respx.mock
def test_http_date_retry_after_is_relative_to_now():
    now = datetime(2026, 10, 15, 12, 0, 0, tzinfo=UTC)
    header = format_datetime(datetime(2026, 10, 15, 12, 0, 30, tzinfo=UTC), usegmt=True)
    assert _retry_after_sleeps(header, now=lambda: now) == [30.0]


@respx.mock
def test_non_json_200_raises_api_error():
    route("profile").mock(return_value=httpx.Response(200, text="<html>captive portal</html>"))
    with pytest.raises(ApiError) as exc:
        make().get_profile()
    assert exc.value.status == 200 and "non-JSON response" in exc.value.message and "captive portal" in exc.value.message


@respx.mock
def test_non_dict_json_200_raises_api_error():
    route("profile").mock(return_value=httpx.Response(200, content=b"null", headers={"Content-Type": "application/json"}))
    with pytest.raises(ApiError) as exc:
        make().get_profile()
    assert exc.value.status == 200 and "unexpected JSON type NoneType" in exc.value.message


@respx.mock
def test_repeated_page_token_raises_instead_of_looping():
    route("dataTypes/steps/dataPoints:reconcile").mock(
        return_value=httpx.Response(200, json={"dataPoints": [], "nextPageToken": "same"})
    )
    with pytest.raises(ApiError) as exc:
        list(make().iter_points(BY_NAME["steps"], START, END))
    assert exc.value.status == 0 and "pagination loop on steps" in exc.value.message


@respx.mock
def test_network_outage_trips_breaker_and_skips_later_requests():
    r = route("profile").mock(side_effect=httpx.ConnectError("boom"))
    sleeps = []
    client = make(sleeps=sleeps, max_attempts=2)
    with pytest.raises(ApiError) as exc:
        client.get_profile()
    assert exc.value.status == 0 and "ConnectError: boom" in exc.value.message
    calls = r.call_count
    assert calls == 2
    with pytest.raises(ApiError) as exc2:
        client.get_profile()
    assert "network unavailable" in exc2.value.message
    assert r.call_count == calls and len(sleeps) == 1


@respx.mock
def test_5xx_exhaustion_does_not_trip_breaker():
    r = route("profile").mock(return_value=httpx.Response(503))
    client = make(max_attempts=2)
    for _ in range(2):
        with pytest.raises(ApiError):
            client.get_profile()
    assert r.call_count == 4


def test_close_only_closes_owned_client():
    injected = httpx.Client()
    HealthClient(Tokens(), http=injected).close()
    assert not injected.is_closed
    injected.close()
    with HealthClient(Tokens()) as owned:
        http = owned._http
    assert http.is_closed


@respx.mock
def test_error_message_includes_google_status_and_path():
    route("profile").mock(return_value=httpx.Response(
        403, json={"error": {"code": 403, "message": "scope missing", "status": "PERMISSION_DENIED"}}))
    with pytest.raises(ApiError) as exc:
        make().get_profile()
    assert "PERMISSION_DENIED: scope missing" in exc.value.message
    assert exc.value.message.startswith("/users/me/profile: ")


@respx.mock
def test_empty_error_body_uses_reason_phrase():
    route("profile").mock(return_value=httpx.Response(404))
    with pytest.raises(ApiError) as exc:
        make().get_profile()
    assert "Not Found" in exc.value.message


@respx.mock
def test_persistent_401_raises_reauth_required():
    r = route("profile").mock(return_value=httpx.Response(401))
    tokens = Tokens()
    with pytest.raises(ReauthRequired) as exc:
        make(tokens=tokens).get_profile()
    assert "still unauthorized after token refresh" in str(exc.value)
    assert r.call_count == 2 and tokens.calls == [False, True]


@respx.mock
@pytest.mark.parametrize("method,suffix", [("reconcile", ":reconcile"), ("list", "")])
def test_data_source_family_is_sent_when_set(method, suffix):
    from dataclasses import replace

    dt = replace(BY_NAME["steps"], method=method, data_source_family="users/me/dataSourceFamilies/google-wearables")
    r = route(f"dataTypes/steps/dataPoints{suffix}").mock(return_value=httpx.Response(200, json={}))
    list(make().iter_points(dt, START, END))
    assert r.calls[0].request.url.params["dataSourceFamily"] == "users/me/dataSourceFamilies/google-wearables"


@respx.mock
def test_data_source_family_is_omitted_by_default():
    r = route("dataTypes/steps/dataPoints:reconcile").mock(return_value=httpx.Response(200, json={}))
    list(make().iter_points(BY_NAME["steps"], START, END))
    assert "dataSourceFamily" not in r.calls[0].request.url.params
