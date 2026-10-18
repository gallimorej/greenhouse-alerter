import json
import logging
from dataclasses import replace
from datetime import time, timedelta

import pytest

from greenhouse import appconfig
from greenhouse.appconfig import AppConfig
from greenhouse.auth import AuthError, IapVerifier, SchedulerVerifier, parse_allowlist
from greenhouse.controller import Settings
from greenhouse.logging_setup import JsonFormatter
from tests.fakes import utc

# --- appconfig -----------------------------------------------------------------------------


def test_defaults_are_safe():
    config = appconfig.from_dict(None)
    assert config.actuation_enabled is False
    assert config.active_sensor_id is None


def test_round_trip_preserves_every_field():
    config = AppConfig(
        controller=Settings(high_f=98.5, low_f=88, active_start=time(5, 30), active_end=time(21, 0),
                            stale_after=timedelta(minutes=15), cycle_duration_minutes=3),
        actuation_enabled=True,
        active_sensor_id="s1",
        reassert_after=timedelta(minutes=45),
        sensor_poll_interval=timedelta(minutes=3),
    )
    data = appconfig.to_dict(config)
    assert data["controller"]["active_start"] == "05:30"
    assert data["controller"]["stale_after_minutes"] == 15
    assert appconfig.from_dict(data) == config


def test_partial_document_fills_defaults():
    config = appconfig.from_dict({"active_sensor_id": "s1", "controller": {"high_f": 99}})
    assert config.active_sensor_id == "s1"
    assert config.controller.high_f == 99 and config.controller.low_f == 90


def test_invalid_stored_settings_raise():
    with pytest.raises(ValueError):
        appconfig.from_dict({"controller": {"high_f": 80, "low_f": 90}})


def test_event_for():
    config = replace(AppConfig(), ifttt_on_event="on_evt", ifttt_off_event="off_evt")
    assert (config.event_for(True), config.event_for(False)) == ("on_evt", "off_evt")


# --- auth ----------------------------------------------------------------------------------

@pytest.fixture
def claims(monkeypatch):
    holder = {}

    def fake_verify(token, request, audience=None, certs_url=None, **_):
        holder["audience"] = audience
        holder["certs_url"] = certs_url
        if token == "invalid":
            raise ValueError("signature mismatch")
        return holder["claims"]

    monkeypatch.setattr("google.oauth2.id_token.verify_oauth2_token", fake_verify)
    monkeypatch.setattr("google.oauth2.id_token.verify_token", fake_verify)
    return holder


SCHED = SchedulerVerifier(audience="https://worker", service_account="sched@p.iam.gserviceaccount.com")
GOOD_OIDC = {"iss": "https://accounts.google.com", "email": "sched@p.iam.gserviceaccount.com", "email_verified": True}


def test_scheduler_token_accepted(claims):
    claims["claims"] = GOOD_OIDC
    assert SCHED("tok") == "sched@p.iam.gserviceaccount.com"
    assert claims["audience"] == "https://worker"


@pytest.mark.parametrize(
    "override",
    [{"email": "someone@p.iam.gserviceaccount.com"}, {"email_verified": False}, {"iss": "https://evil"}],
)
def test_scheduler_token_rejected(claims, override):
    claims["claims"] = GOOD_OIDC | override
    with pytest.raises(AuthError):
        SCHED("tok")


def test_bad_signature_rejected(claims):
    with pytest.raises(AuthError):
        SCHED("invalid")


IAP = IapVerifier(audience="/projects/1/locations/us-east1/services/greenhouse-admin",
                  allowed_emails=parse_allowlist("Jeff@Example.com, other@example.com"))


def test_iap_assertion_accepted_case_insensitively(claims):
    claims["claims"] = {"iss": "https://cloud.google.com/iap", "email": "jeff@example.com"}
    assert IAP("tok") == "jeff@example.com"
    assert claims["certs_url"] == "https://www.gstatic.com/iap/verify/public_key"


@pytest.mark.parametrize(
    "claim",
    [{"iss": "https://cloud.google.com/iap", "email": "stranger@example.com"},
     {"iss": "https://accounts.google.com", "email": "jeff@example.com"},
     {"iss": "https://cloud.google.com/iap"}],
)
def test_iap_assertion_rejected(claims, claim):
    claims["claims"] = claim
    with pytest.raises(AuthError):
        IAP("tok")


def test_parse_allowlist():
    assert parse_allowlist(" A@x ,, b@y ") == frozenset({"a@x", "b@y"})


# --- logging -------------------------------------------------------------------------------

def test_json_formatter_emits_severity_and_structured_fields():
    record = logging.LogRecord("greenhouse.worker", logging.WARNING, __file__, 1, "tick refused", None, None)
    record.json = {"event": "tick", "sensor_fresh": False, "at": utc(2026, 7, 15, 16, 20)}
    entry = json.loads(JsonFormatter().format(record))
    assert entry["severity"] == "WARNING" and entry["message"] == "tick refused"
    assert entry["event"] == "tick" and entry["sensor_fresh"] is False
    assert entry["at"].startswith("2026-07-15T16:20")
