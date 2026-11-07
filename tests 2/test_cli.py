import sys

import pytest
from sqlalchemy import select

from app import cli, db as database
from app.config import get_settings
from app.models import ApiKey, AuditLog, Base
from app.security import hash_key


@pytest.fixture()
def cli_db(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "database_url", f"sqlite:///{tmp_path}/cli.db")
    monkeypatch.setattr(get_settings(), "env", "development")
    yield


def run(*argv):
    sys.argv = ["cli", *argv]
    cli.main()


def test_create_list_and_revoke_keys(cli_db, capsys):
    with pytest.raises(SystemExit):                                  # an internal key must be bound to a tenant
        run("create-key", "n8n-prod", "--scope", "internal")
    run("create-key", "wisecp-module", "--scope", "admin", "--ip", "203.0.113.9", "--days", "30")
    raw = [l for l in capsys.readouterr().out.splitlines() if l.startswith("Key")][0].split(": ")[1].strip()
    run("list-keys")
    out = capsys.readouterr().out
    assert "wisecp-module" in out and "active" in out and raw not in out          # the secret is never listed
    run("revoke-key", "wisecp-module")
    with database.SessionLocal() as s:
        k = s.scalars(select(ApiKey).where(ApiKey.key_hash == hash_key(raw))).one()
        assert k.revoked is True
        assert s.scalars(select(AuditLog).where(AuditLog.action == "apikey.revoke")).one()
    run("list-keys")
    assert "REVOKED" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        run("revoke-key", "no-such-key")


def test_enable_currencies_refuses_metals(cli_db):
    run("seed-plans")
    run("enable-currencies", "NGN", "usd")
    with pytest.raises(SystemExit):
        run("enable-currencies", "XAU")
