"""Legacy transport tests exercise their own authentication modes explicitly."""
import pytest


@pytest.fixture(autouse=True)
def account_mode(monkeypatch, tmp_path):
    monkeypatch.setenv('ACCOUNT_AUTH_ENABLED', '0')
    monkeypatch.setenv('ACCOUNT_DB', str(tmp_path / 'accounts.sqlite3'))
