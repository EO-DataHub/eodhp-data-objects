"""Anonymous preview images (ADR-003, decision 6)."""

import dataclasses
import json
import logging
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient

from data_objects.app import THUMBNAIL_SESSION, create_app
from data_objects.auth import Verifier
from data_objects.settings import ObjectRef, Settings, Thumbnails
from data_objects.signer import Signer, _s3_client
from tests.conftest import LANDSAT, ROLE, Clock, FakeS3, FakeSTS, s3_error

THUMBNAIL_ROLE = "arn:aws:iam::123456789012:role/data-objects-thumbnails"
SCENE = "collection02/level-2/standard/oli-tirs/2024/092/079/LC08_X/LC08_X"
SMALL = f"/api/data/objects/usgs-landsat/{SCENE}_thumb_small.jpeg"
LARGE = f"/api/data/objects/usgs-landsat/{SCENE}_thumb_large.jpeg"
DATA = f"/api/data/objects/usgs-landsat/{SCENE}_SR_B4.TIF"
THUMBNAIL_READINESS_KEY = "collection02/x/X_thumb_small.jpeg"


@pytest.fixture
def thumbnail_settings(settings: Settings) -> Settings:
    return dataclasses.replace(
        settings,
        thumbnails=Thumbnails(
            role_arn=THUMBNAIL_ROLE, readiness_object=ObjectRef(LANDSAT.name, THUMBNAIL_READINESS_KEY)
        ),
    )


@pytest.fixture
def thumbnail_signer(
    thumbnail_settings: Settings,
    sts: FakeSTS,
    clock: Clock,
    s3_clients: list[FakeS3],
    s3_errors: dict[str, ClientError],
) -> Signer:
    def factory(credentials: dict[str, Any], region: str) -> FakeS3:
        s3_clients.append(FakeS3(_s3_client(credentials, region), s3_errors))
        return s3_clients[-1]

    return Signer(
        THUMBNAIL_ROLE,
        url_lifetime=thumbnail_settings.url_lifetime,
        session_duration=thumbnail_settings.session_duration,
        refresh_margin=thumbnail_settings.refresh_margin,
        sts=sts,
        now=clock,
        s3_factory=factory,
    )


@pytest.fixture
def thumbnail_client(
    thumbnail_settings: Settings, signer: Signer, thumbnail_signer: Signer, verifier: Verifier
) -> TestClient:
    app = create_app(thumbnail_settings, signer=signer, verifier=verifier, thumbnail_signer=thumbnail_signer)
    return TestClient(app, follow_redirects=False)


@pytest.mark.parametrize("path", [SMALL, LARGE])
def test_preview_needs_no_token(thumbnail_client: TestClient, path: str) -> None:
    response = thumbnail_client.get(path)

    assert response.status_code == 307
    url = urlparse(response.headers["location"])
    assert url.netloc == "usgs-landsat.s3.us-west-2.amazonaws.com"
    assert parse_qs(url.query)["x-amz-request-payer"] == ["requester"]


def test_preview_is_signed_by_the_thumbnail_role(thumbnail_client: TestClient, sts: FakeSTS) -> None:
    thumbnail_client.get(SMALL)
    assert [(c["RoleArn"], c["RoleSessionName"]) for c in sts.calls] == [(THUMBNAIL_ROLE, THUMBNAIL_SESSION)]


def test_token_is_ignored_for_a_preview(thumbnail_client: TestClient, sts: FakeSTS, token) -> None:
    # The Hub pays for previews whoever views them, so a workspace is never charged.
    response = thumbnail_client.get(
        SMALL, headers={"Authorization": f"Bearer {token('a-ws')}", "X-EODH-Workspace": "a-ws"}
    )
    assert response.status_code == 307
    assert [c["RoleSessionName"] for c in sts.calls] == [THUMBNAIL_SESSION]


def test_even_an_invalid_token_is_ignored_for_a_preview(thumbnail_client: TestClient) -> None:
    assert thumbnail_client.get(SMALL, headers={"Authorization": "Bearer not-a-jwt"}).status_code == 307


def test_preview_redirect_may_be_cached_for_less_than_the_url_lifetime(thumbnail_client: TestClient) -> None:
    response = thumbnail_client.get(SMALL)
    assert response.headers["cache-control"] == "max-age=300"
    assert parse_qs(urlparse(response.headers["location"]).query)["X-Amz-Expires"] == ["900"]


def test_data_still_needs_a_token(thumbnail_client: TestClient) -> None:
    assert thumbnail_client.get(DATA).status_code == 401


def test_data_is_still_signed_by_the_signer_role(thumbnail_client: TestClient, sts: FakeSTS, token) -> None:
    response = thumbnail_client.get(DATA, headers={"Authorization": f"Bearer {token('a-ws')}"})
    assert response.status_code == 307
    assert response.headers["cache-control"] == "no-store"
    assert [(c["RoleArn"], c["RoleSessionName"]) for c in sts.calls] == [(ROLE, "a-ws")]


@pytest.mark.parametrize(
    "key",
    [f"{SCENE}_thumb_small.jpeg.TIF", f"{SCENE}_thumb_small.JPEG", f"{SCENE}_thumb_small"],
    ids=["suffix not at the end", "different case", "no extension"],
)
def test_only_an_exact_suffix_is_a_preview(thumbnail_client: TestClient, key: str) -> None:
    assert thumbnail_client.get(f"/api/data/objects/usgs-landsat/{key}").status_code == 401


def test_preview_in_a_bucket_not_on_the_allowlist_is_404(thumbnail_client: TestClient) -> None:
    assert thumbnail_client.get("/api/data/objects/other-bucket/x_thumb_small.jpeg").status_code == 404


def test_preview_head_needs_no_token(thumbnail_client: TestClient, s3_clients: list[FakeS3]) -> None:
    response = thumbnail_client.head(SMALL)
    assert response.status_code == 200
    assert "location" not in response.headers
    assert s3_clients[0].heads[0]["Key"] == f"{SCENE}_thumb_small.jpeg"


def test_missing_preview_head_is_404(thumbnail_client: TestClient, s3_errors: dict[str, ClientError]) -> None:
    s3_errors[f"{SCENE}_thumb_small.jpeg"] = s3_error(403, "403")
    assert thumbnail_client.head(SMALL).status_code == 404


def test_preview_is_logged_without_a_workspace(thumbnail_client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    thumbnail_client.get(SMALL)

    record = next(json.loads(r.getMessage()) for r in caplog.records if r.name == "data_objects.access")
    assert (record["status"], record["thumbnail"]) == (307, True)
    assert "workspace" not in record


def test_without_thumbnails_a_preview_needs_a_token(client: TestClient) -> None:
    assert client.get(SMALL).status_code == 401


def test_ready_only_when_both_roles_can_read(
    thumbnail_client: TestClient, s3_errors: dict[str, ClientError], sts: FakeSTS, caplog: pytest.LogCaptureFixture
) -> None:
    s3_errors[THUMBNAIL_READINESS_KEY] = s3_error(403, "403")
    with thumbnail_client:
        assert thumbnail_client.get("/readyz").status_code == 503
        assert "for the thumbnail role" in caplog.text

        del s3_errors[THUMBNAIL_READINESS_KEY]
        assert thumbnail_client.get("/readyz").status_code == 200
    assert {c["RoleArn"] for c in sts.calls} == {ROLE, THUMBNAIL_ROLE}


def test_signer_and_settings_must_agree(settings: Settings, signer: Signer, verifier: Verifier) -> None:
    with pytest.raises(ValueError):
        create_app(settings, signer=signer, verifier=verifier, thumbnail_signer=signer)


def test_cache_max_age_follows_settings(
    thumbnail_settings: Settings, signer: Signer, thumbnail_signer: Signer, verifier: Verifier
) -> None:
    assert thumbnail_settings.thumbnails is not None
    settings = dataclasses.replace(
        thumbnail_settings, thumbnails=dataclasses.replace(thumbnail_settings.thumbnails, cache_max_age=timedelta(0))
    )
    app = create_app(settings, signer=signer, verifier=verifier, thumbnail_signer=thumbnail_signer)
    response = TestClient(app, follow_redirects=False).get(SMALL)
    assert response.headers["cache-control"] == "max-age=0"
