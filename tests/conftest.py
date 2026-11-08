import pytest


@pytest.fixture(autouse=True)
def isolate_monitoring_credentials(monkeypatch):
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    monkeypatch.delenv("RAINDROP_WRITE_KEY", raising=False)
