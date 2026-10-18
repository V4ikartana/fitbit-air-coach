import pytest

from fitbit_air.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "health.db")
    yield s
    s.close()
