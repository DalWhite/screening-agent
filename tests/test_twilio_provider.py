"""TwilioProvider unit tests — the SDK's REST client is always replaced."""

import json

from twilio.request_validator import RequestValidator

from backend.channels import twilio_provider as tp_module
from backend.channels.twilio_provider import TwilioProvider
from backend.config import settings


def test_unconfigured_provider_reports_and_skips_send(monkeypatch):
    monkeypatch.setattr(settings, "twilio_account_sid", "")
    monkeypatch.setattr(settings, "twilio_auth_token", "")
    provider = TwilioProvider(config=settings)
    assert provider.configured is False
    assert provider.send_whatsapp("+34600111222", "hola") is None


def test_send_whatsapp_adds_channel_prefixes(provider, fake_twilio_client):
    sid = provider.send_whatsapp("+34600111222", "hola")
    assert sid and sid.startswith("SM")
    sent = fake_twilio_client.messages.sent[0]
    assert sent["to"] == "whatsapp:+34600111222"
    assert sent["from_"] == "whatsapp:+14155238886"
    assert sent["body"] == "hola"

    provider.send_whatsapp("whatsapp:+34600111222", "ya con prefijo")
    assert fake_twilio_client.messages.sent[1]["to"] == "whatsapp:+34600111222"


def test_send_whatsapp_swallows_api_errors(provider, fake_twilio_client):
    fake_twilio_client.messages.fail = True
    assert provider.send_whatsapp("+34600111222", "hola") is None


def test_lazy_client_built_with_credentials(twilio_config, monkeypatch):
    captured = {}

    class SpyClient:
        def __init__(self, sid, token):
            captured["creds"] = (sid, token)

    monkeypatch.setattr(tp_module, "Client", SpyClient)
    provider = TwilioProvider(config=twilio_config)
    _ = provider.client
    assert captured["creds"] == (twilio_config.twilio_account_sid, twilio_config.twilio_auth_token)


def test_signature_validation_accepts_real_twilio_signature(twilio_config, monkeypatch):
    monkeypatch.setattr(settings, "twilio_validate_signatures", True)
    provider = TwilioProvider(config=settings)
    url = "https://screening.example.com/api/channels/whatsapp/inbound"
    params = {"From": "whatsapp:+34600111222", "Body": "hola"}
    good = RequestValidator(settings.twilio_auth_token).compute_signature(url, params)
    assert provider.validate_signature(url, params, good) is True
    assert provider.validate_signature(url, params, "forged-signature") is False
    assert provider.validate_signature(url, {**params, "Body": "tampered"}, good) is False


def test_signature_validation_can_be_disabled(twilio_config, monkeypatch):
    monkeypatch.setattr(settings, "twilio_validate_signatures", False)
    provider = TwilioProvider(config=settings)
    assert provider.validate_signature("http://x", {}, "whatever") is True


def test_signature_validation_rejects_without_auth_token(monkeypatch):
    monkeypatch.setattr(settings, "twilio_auth_token", "")
    monkeypatch.setattr(settings, "twilio_validate_signatures", True)
    provider = TwilioProvider(config=settings)
    assert provider.validate_signature("http://x", {}, "sig") is False


def test_api_key_auth_builds_client_with_account_sid(twilio_config, monkeypatch):
    captured = {}

    class SpyClient:
        def __init__(self, username, password, account_sid=None):
            captured["args"] = (username, password, account_sid)

    monkeypatch.setattr(tp_module, "Client", SpyClient)
    monkeypatch.setattr(settings, "twilio_auth_token", "")  # no auth token, API key only
    monkeypatch.setattr(settings, "twilio_api_key_sid", "SKxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")
    monkeypatch.setattr(settings, "twilio_api_key_secret", "s3cr3t")
    provider = TwilioProvider(config=settings)
    assert provider.configured and provider.uses_api_key
    _ = provider.client
    assert captured["args"] == ("SKxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx", "s3cr3t", settings.twilio_account_sid)


def test_check_credentials_surfaces_auth_failure(twilio_config):
    from types import SimpleNamespace

    class FailingAccounts:
        def __call__(self, sid):
            return self

        def fetch(self):
            raise RuntimeError("authentication failed, auth token is not valid for account")

    fake = SimpleNamespace(api=SimpleNamespace(v2010=SimpleNamespace(accounts=FailingAccounts())))
    provider = TwilioProvider(config=settings, client=fake)
    import pytest
    with pytest.raises(RuntimeError, match="auth token is not valid"):
        provider.check_credentials()


def test_check_credentials_reports_account(twilio_config):
    from types import SimpleNamespace

    class Accounts:
        def __call__(self, sid):
            assert sid == settings.twilio_account_sid
            return self

        def fetch(self):
            return SimpleNamespace(sid=settings.twilio_account_sid, friendly_name="My Trial", status="active", type="Trial")

    fake = SimpleNamespace(api=SimpleNamespace(v2010=SimpleNamespace(accounts=Accounts())))
    info = TwilioProvider(config=settings, client=fake).check_credentials()
    assert info["type"] == "Trial" and info["friendly_name"] == "My Trial"


def test_send_template_passes_content_sid_and_json_variables(provider, fake_twilio_client):
    sid = provider.send_whatsapp("+34600111222", content_sid="HX123", content_variables={"1": "Laura", "2": "Marisol"})
    assert sid
    sent = fake_twilio_client.messages.sent[0]
    assert sent["content_sid"] == "HX123"
    assert sent["body"] is None
    assert json.loads(sent["content_variables"]) == {"1": "Laura", "2": "Marisol"}


def test_template_required_error_is_flagged_not_raised(provider, fake_twilio_client):
    fake_twilio_client.messages.reject_freeform_with = 21654
    assert provider.send_whatsapp("+34600111222", "hola") is None
    assert provider.template_required is True
    assert provider.last_error_code == 21654
    # a successful template send clears the flag
    assert provider.send_whatsapp("+34600111222", content_sid="HX123")
    assert provider.template_required is False
