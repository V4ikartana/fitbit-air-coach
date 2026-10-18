import fitbit_air
from fitbit_air.errors import ApiError


def test_version():
    assert fitbit_air.__version__ == "0.1.0"


def test_api_error_carries_status():
    err = ApiError(403, "scope missing")
    assert err.status == 403
    assert str(err) == "HTTP 403: scope missing"
