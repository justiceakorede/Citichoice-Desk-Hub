import pytest
from app.config import Settings

GOOD = dict(env="production", database_url="postgresql://u:p@db/hub", secret_key="x" * 40,
            chatwoot_url="https://desk.example", chatwoot_platform_token="t",
            oidc_issuer="https://accounts.example", oidc_client_id="cid", oidc_client_secret="shh",
            oidc_redirect_uri="https://deskhub.example/auth/callback", oidc_project_id="123",
            staff_ui_host="deskhub.example", api_host="hub.example")


def prod(**kw):
    return Settings(_env_file=None, **{**GOOD, **kw})


def test_good_production_config_passes():
    prod().validate_for_runtime()


@pytest.mark.parametrize("kw", [
    dict(database_url=""), dict(database_url="sqlite:///x.db"), dict(secret_key="short"), dict(chatwoot_url=""),
    dict(oidc_issuer=""), dict(oidc_issuer="http://accounts.example"), dict(oidc_client_secret=""), dict(oidc_client_id=""),
    dict(oidc_project_id=""), dict(staff_ui_host=""), dict(api_host=""),
    dict(oidc_redirect_uri="https://elsewhere.example/auth/callback"),
    dict(oidc_redirect_uri="http://deskhub.example/auth/callback"),
    dict(api_host="deskhub.example"),
])
def test_unsafe_production_config_refuses_to_boot(kw):
    with pytest.raises(RuntimeError):
        prod(**kw).validate_for_runtime()


def test_production_has_no_sqlite_fallback():
    with pytest.raises(RuntimeError):
        prod(database_url="").effective_database_url


def test_customer_api_is_off_by_default():
    assert prod().customer_api_enabled is False
