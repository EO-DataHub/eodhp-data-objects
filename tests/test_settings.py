import json
from datetime import timedelta

import pytest

from data_objects.settings import Bucket, ObjectRef, Settings, SettingsError, load_settings

BUCKETS = json.dumps([{"name": "usgs-landsat", "region": "us-west-2", "requester_pays": True}])

ENV = {
    "KEYCLOAK_BASE_URL": "https://hub.example/",
    "SIGNER_ROLE_ARN": "arn:aws:iam::123456789012:role/data-objects-signer",
    "BUCKETS": BUCKETS,
    "READINESS_OBJECT": "usgs-landsat/collection02/x/X_MTL.json",
}


def test_loads_with_the_adr_defaults() -> None:
    settings = load_settings(ENV)

    assert settings.keycloak_issuer == "https://hub.example/keycloak/realms/eodhp"
    assert settings.buckets == {"usgs-landsat": Bucket("usgs-landsat", "us-west-2", requester_pays=True)}
    assert settings.readiness_object == ObjectRef("usgs-landsat", "collection02/x/X_MTL.json")
    assert settings.url_lifetime == timedelta(minutes=15)
    assert settings.session_duration == timedelta(hours=1)
    assert settings.refresh_margin == timedelta(minutes=20)


def test_lifetime_session_and_margin_are_configurable() -> None:
    settings = load_settings(
        ENV
        | {
            "URL_LIFETIME_SECONDS": "3600",
            "SESSION_DURATION_SECONDS": "10800",
            "SESSION_REFRESH_MARGIN_SECONDS": "3600",
        }
    )
    assert (settings.url_lifetime, settings.session_duration, settings.refresh_margin) == (
        timedelta(hours=1),
        timedelta(hours=3),
        timedelta(hours=1),
    )


@pytest.mark.parametrize("name", ["KEYCLOAK_BASE_URL", "SIGNER_ROLE_ARN", "BUCKETS", "READINESS_OBJECT"])
def test_required_setting_missing_is_refused(name: str) -> None:
    with pytest.raises(SettingsError, match=name):
        load_settings({k: v for k, v in ENV.items() if k != name})


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"URL_LIFETIME_SECONDS": "1500"}, "longer than the session refresh margin"),
        ({"URL_LIFETIME_SECONDS": "0"}, "must be positive"),
        ({"SESSION_REFRESH_MARGIN_SECONDS": "3600"}, "shorter than the session duration"),
        ({"SESSION_DURATION_SECONDS": "86400"}, "12 hours"),
        ({"URL_LIFETIME_SECONDS": "15m"}, "whole number of seconds"),
        ({"BUCKETS": "[]"}, "allowlist is empty"),
        ({"BUCKETS": '[{"name": "x"}]'}, "JSON list"),
        ({"BUCKETS": BUCKETS[:-1] + ', {"name": "usgs-landsat", "region": "x", "requester_pays": false}]'}, "twice"),
        ({"READINESS_OBJECT": "other-bucket/key"}, "not in an allowlisted bucket"),
        ({"READINESS_OBJECT": "usgs-landsat"}, "<bucket>/<key>"),
    ],
)
def test_inconsistent_settings_stop_startup(overrides: dict[str, str], message: str) -> None:
    with pytest.raises(SettingsError, match=message):
        load_settings(ENV | overrides)


def test_settings_check_their_rules_when_built_directly() -> None:
    with pytest.raises(SettingsError):
        Settings(
            keycloak_issuer="https://hub.example/keycloak/realms/eodhp",
            signer_role_arn="arn",
            buckets={"b": Bucket("b", "eu-west-2", requester_pays=False)},
            readiness_object=ObjectRef("b", "k"),
            url_lifetime=timedelta(minutes=30),
        )
