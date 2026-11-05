from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
import pytest
from botocore.exceptions import ClientError
from cryptography.hazmat.primitives.asymmetric import rsa
from dateutil.tz import tzutc
from fastapi.testclient import TestClient

from data_objects.app import create_app
from data_objects.auth import Verifier
from data_objects.settings import Bucket, ObjectRef, Settings
from data_objects.signer import Signer, _s3_client

ROLE = "arn:aws:iam::123456789012:role/data-objects-signer"
ISSUER = "https://hub.example/keycloak/realms/eodhp"
T0 = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)

LANDSAT = Bucket(name="usgs-landsat", region="us-west-2", requester_pays=True)
OPEN_BUCKET = Bucket(name="hub-open-data", region="eu-west-2", requester_pays=False)
READINESS_KEY = "collection02/level-2/x/X_MTL.json"


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


class FakeSTS:
    """Records AssumeRole calls and returns credentials valid for the requested duration."""

    def __init__(self, clock: Clock) -> None:
        self.calls: list[dict[str, Any]] = []
        self._clock = clock

    def assume_role(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {
            "Credentials": {
                "AccessKeyId": "ASIAEXAMPLE",
                "SecretAccessKey": "secret",
                "SessionToken": f"token-{len(self.calls)}",
                "Expiration": self._clock() + timedelta(seconds=kwargs["DurationSeconds"]),
            }
        }


def s3_error(status: int, code: str) -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": ""}, "ResponseMetadata": {"HTTPStatusCode": status}},  # pyright: ignore[reportArgumentType]
        "HeadObject",
    )


class FakeS3:
    """The real S3 client for presigning, which is local, so the URLs under test are the ones
    boto3 really produces. head_object is canned: `errors` maps a key to the S3 error it gets,
    and any other key exists."""

    def __init__(self, real: Any, errors: dict[str, ClientError]) -> None:
        self._real = real
        self._errors = errors
        self.heads: list[dict[str, Any]] = []

    def generate_presigned_url(self, *args: Any, **kwargs: Any) -> str:
        return self._real.generate_presigned_url(*args, **kwargs)

    def head_object(self, **kwargs: Any) -> dict[str, Any]:
        self.heads.append(kwargs)
        if kwargs["Key"] in self._errors:
            raise self._errors[kwargs["Key"]]
        return {
            "ContentLength": 71155923,
            "ContentType": "image/tiff",
            "ETag": '"abc"',
            "LastModified": datetime(2026, 11, 2, 0, 0, tzinfo=tzutc()),  # what boto3 returns
        }


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def sts(clock: Clock) -> FakeSTS:
    return FakeSTS(clock)


@pytest.fixture
def s3_errors() -> dict[str, ClientError]:
    """Keys whose HeadObject fails. Missing keys get 403, as they do without s3:ListBucket."""
    return {"x/missing": s3_error(403, "403")}


@pytest.fixture
def s3_clients() -> list[FakeS3]:
    return []


@pytest.fixture
def settings() -> Settings:
    return Settings(
        keycloak_issuer=ISSUER,
        signer_role_arn=ROLE,
        buckets={b.name: b for b in (LANDSAT, OPEN_BUCKET)},
        readiness_object=ObjectRef(LANDSAT.name, READINESS_KEY),
    )


@pytest.fixture
def signer(
    settings: Settings, sts: FakeSTS, clock: Clock, s3_clients: list[FakeS3], s3_errors: dict[str, ClientError]
) -> Signer:
    def factory(credentials: dict[str, Any], region: str) -> FakeS3:
        s3_clients.append(FakeS3(_s3_client(credentials, region), s3_errors))
        return s3_clients[-1]

    return Signer(
        ROLE,
        url_lifetime=settings.url_lifetime,
        session_duration=settings.session_duration,
        refresh_margin=settings.refresh_margin,
        sts=sts,
        now=clock,
        s3_factory=factory,
    )


@pytest.fixture(scope="session")
def private_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def verifier(private_key: rsa.RSAPrivateKey) -> Verifier:
    public = private_key.public_key()
    return Verifier(issuer=ISSUER, audience=["eodh", "eodh-workspaces"], key_for=lambda _token: public)


@pytest.fixture
def token(private_key: rsa.RSAPrivateKey):
    """Mint a token with the claims a Keycloak token carries, listing the given workspaces."""

    def make(*workspaces: str, sub: str = "user-1", hours: int = 1, **claims: Any) -> str:
        now = datetime.now(tz=UTC)
        payload = {
            "iss": ISSUER,
            "aud": ["eodh", "account"],
            "azp": "eodh-workspaces",
            "sub": sub,
            "iat": now,
            "exp": now + timedelta(hours=hours),
            "workspaces": list(workspaces),
        } | claims
        return jwt.encode(payload, private_key, algorithm="RS256")

    return make


@pytest.fixture
def client(settings: Settings, signer: Signer, verifier: Verifier) -> TestClient:
    return TestClient(create_app(settings, signer=signer, verifier=verifier), follow_redirects=False)
