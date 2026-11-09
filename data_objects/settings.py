"""Settings, read from the environment once at startup.

Every rule between the settings is checked here, so a bad configuration stops the process
before it serves a request (ADR-003, decision 5).
"""

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta

# STS refuses a role session longer than 12 hours.
MAX_SESSION_DURATION = timedelta(hours=12)

DEFAULT_URL_LIFETIME = timedelta(minutes=15)
DEFAULT_SESSION_DURATION = timedelta(hours=1)
DEFAULT_REFRESH_MARGIN = timedelta(minutes=20)


class SettingsError(ValueError):
    pass


@dataclass(frozen=True)
class Bucket:
    """An allowlisted bucket. The endpoint signs reads only for these."""

    name: str
    region: str
    requester_pays: bool


@dataclass(frozen=True)
class ObjectRef:
    bucket: str
    key: str

    @classmethod
    def parse(cls, value: str) -> "ObjectRef":
        bucket, _, key = value.partition("/")
        if not bucket or not key:
            msg = f"Expected <bucket>/<key>, got {value!r}"
            raise SettingsError(msg)
        return cls(bucket, key)


@dataclass(frozen=True)
class Settings:
    keycloak_issuer: str
    signer_role_arn: str
    buckets: Mapping[str, Bucket]
    # An object that must exist, read at startup to prove that the signer role works. Without
    # s3:ListBucket, a broken role and a missing key both give 403, so this is the only way to
    # tell them apart.
    readiness_object: ObjectRef
    url_lifetime: timedelta = DEFAULT_URL_LIFETIME
    session_duration: timedelta = DEFAULT_SESSION_DURATION
    # A workspace's session is replaced when less than this remains. It must be at least the
    # URL lifetime, so that every URL is valid for its full lifetime.
    refresh_margin: timedelta = DEFAULT_REFRESH_MARGIN
    # The Keycloak client IDs platform tokens are issued for. The same list is in
    # accounting-service's authz.py; change them together.
    jwt_audience: tuple[str, ...] = ("eodh", "eodh-workspaces")

    def __post_init__(self) -> None:
        if not self.buckets:
            raise SettingsError("The bucket allowlist is empty")
        if self.url_lifetime <= timedelta(0):
            raise SettingsError("The URL lifetime must be positive")
        if self.url_lifetime > self.refresh_margin:
            raise SettingsError(
                "The URL lifetime is longer than the session refresh margin, so a URL could expire early"
            )
        if self.refresh_margin >= self.session_duration:
            raise SettingsError("The session refresh margin must be shorter than the session duration")
        if self.session_duration > MAX_SESSION_DURATION:
            raise SettingsError("STS sessions cannot last longer than 12 hours")
        if self.readiness_object.bucket not in self.buckets:
            raise SettingsError("The readiness object is not in an allowlisted bucket")


def _parse_buckets(raw: str) -> dict[str, Bucket]:
    """`BUCKETS` is a JSON list: [{"name": ..., "region": ..., "requester_pays": true}, ...]."""
    try:
        entries = json.loads(raw)
        buckets = [Bucket(name=e["name"], region=e["region"], requester_pays=e["requester_pays"]) for e in entries]
    except (ValueError, TypeError, KeyError) as e:
        msg = f"BUCKETS is not a JSON list of {{name, region, requester_pays}}: {e}"
        raise SettingsError(msg) from e
    # Not bool(): "false" in quotes would turn requester-pays on, and the Hub would pay for
    # reads the bucket owner should pay for.
    for b in buckets:
        if not isinstance(b.requester_pays, bool):
            msg = f"BUCKETS: requester_pays for {b.name} must be true or false, got {b.requester_pays!r}"
            raise SettingsError(msg)
    by_name = {b.name: b for b in buckets}
    if len(by_name) != len(buckets):
        raise SettingsError("BUCKETS names a bucket twice")
    return by_name


def _required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        msg = f"{name} must be set"
        raise SettingsError(msg)
    return value


def _seconds(env: Mapping[str, str], name: str, default: timedelta) -> timedelta:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        return timedelta(seconds=int(raw))
    except ValueError as e:
        msg = f"{name} must be a whole number of seconds"
        raise SettingsError(msg) from e


def load_settings(env: Mapping[str, str] = os.environ) -> Settings:
    # The issuer is compared with the token's `iss` exactly, so a trailing slash would refuse
    # every token. Built the same way as accounting-service's.
    base_url = _required(env, "KEYCLOAK_BASE_URL").rstrip("/")
    realm = env.get("KEYCLOAK_REALM", "eodhp").strip() or "eodhp"

    return Settings(
        keycloak_issuer=f"{base_url}/keycloak/realms/{realm}",
        signer_role_arn=_required(env, "SIGNER_ROLE_ARN"),
        buckets=_parse_buckets(_required(env, "BUCKETS")),
        readiness_object=ObjectRef.parse(_required(env, "READINESS_OBJECT")),
        url_lifetime=_seconds(env, "URL_LIFETIME_SECONDS", DEFAULT_URL_LIFETIME),
        session_duration=_seconds(env, "SESSION_DURATION_SECONDS", DEFAULT_SESSION_DURATION),
        refresh_margin=_seconds(env, "SESSION_REFRESH_MARGIN_SECONDS", DEFAULT_REFRESH_MARGIN),
    )
