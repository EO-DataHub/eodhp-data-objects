import json
import logging

import pytest
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient

from data_objects.app import READINESS_SESSION
from tests.conftest import READINESS_KEY, FakeS3, FakeSTS, s3_error

PREFIX = "/api/data/objects"
PATH = f"{PREFIX}/usgs-landsat/collection02/level-2/x/X_SR_B4.TIF"


def bearer(value: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {value}"}


def access_records(caplog: pytest.LogCaptureFixture) -> list[dict]:
    return [json.loads(r.getMessage()) for r in caplog.records if r.name == "data_objects.access"]


def test_get_redirects_to_a_presigned_url(client: TestClient, token) -> None:
    response = client.get(PATH, headers=bearer(token("a-ws")))

    assert response.status_code == 307
    assert response.headers["location"].startswith(
        "https://usgs-landsat.s3.us-west-2.amazonaws.com/collection02/level-2/x/X_SR_B4.TIF?"
    )
    assert response.headers["cache-control"] == "no-store"


def test_get_does_not_check_that_the_object_exists(client: TestClient, token, s3_clients: list[FakeS3]) -> None:
    # ADR-003 decision 4: GDAL and titiler send HEAD first, so GET skips the round trip.
    response = client.get(f"{PREFIX}/usgs-landsat/x/missing", headers=bearer(token("a-ws")))
    assert response.status_code == 307
    assert [s3.heads for s3 in s3_clients] == [[]]


def test_head_is_answered_without_a_redirect(client: TestClient, token) -> None:
    # A redirected HEAD would leave GDAL holding a HEAD-signed URL for its range GETs.
    response = client.head(PATH, headers=bearer(token("a-ws")))
    assert response.status_code == 200
    assert "location" not in response.headers
    assert response.headers["content-length"] == "71155923"
    assert response.headers["accept-ranges"] == "bytes"


def test_head_of_a_missing_object_is_404_and_logs_the_s3_status(
    client: TestClient, token, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    response = client.head(f"{PREFIX}/usgs-landsat/x/missing", headers=bearer(token("a-ws")))

    assert response.status_code == 404
    record = access_records(caplog)[-1]
    assert (record["status"], record["s3_status"], record["s3_code"]) == (404, 403, "403")


def test_head_that_fails_for_another_reason_is_502(
    client: TestClient, token, s3_errors: dict[str, ClientError]
) -> None:
    s3_errors["x/slow"] = s3_error(503, "SlowDown")
    assert client.head(f"{PREFIX}/usgs-landsat/x/slow", headers=bearer(token("a-ws"))).status_code == 502


def test_head_needs_a_token(client: TestClient) -> None:
    assert client.head(PATH).status_code == 401


@pytest.mark.parametrize("path", [f"{PREFIX}/someone-elses-bucket/x", f"{PREFIX}/usgs-landsat/"])
def test_unknown_bucket_or_empty_key_is_404_even_without_a_token(client: TestClient, path: str) -> None:
    assert client.get(path).status_code == 404


def test_no_token_is_401(client: TestClient) -> None:
    assert client.get(PATH).status_code == 401


def test_ambiguous_workspace_is_400(client: TestClient, token) -> None:
    assert client.get(PATH, headers=bearer(token("a-ws", "b-ws"))).status_code == 400


def test_workspace_not_granted_is_403(client: TestClient, token) -> None:
    response = client.get(PATH, headers=bearer(token("a-ws")) | {"X-EODH-Workspace": "b-ws"})
    assert response.status_code == 403


def test_query_parameter_chooses_the_workspace(client: TestClient, token, sts: FakeSTS) -> None:
    response = client.get(f"{PATH}?workspace=b-ws", headers=bearer(token("a-ws", "b-ws")))
    assert response.status_code == 307
    assert sts.calls[-1]["RoleSessionName"] == "b-ws"


def test_query_parameter_is_not_signed_into_the_s3_url(client: TestClient, token) -> None:
    response = client.get(f"{PATH}?workspace=a-ws", headers=bearer(token("a-ws")))
    assert "workspace=" not in response.headers["location"]


def test_sts_failure_on_get_is_502(client: TestClient, token, sts: FakeSTS) -> None:
    def refuse(**_: object) -> dict:
        raise s3_error(403, "AccessDenied")

    sts.assume_role = refuse  # pyright: ignore[reportAttributeAccessIssue]
    assert client.get(PATH, headers=bearer(token("a-ws"))).status_code == 502


def test_preflight_allows_the_headers_clients_send(client: TestClient) -> None:
    response = client.options(
        PATH,
        headers={
            "Origin": "https://example.org",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization,x-eodh-workspace,range",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] in ("*", "https://example.org")


def test_redirect_carries_cors_headers(client: TestClient, token) -> None:
    response = client.get(PATH, headers=bearer(token("a-ws")) | {"Origin": "https://example.org"})
    assert response.headers["access-control-allow-origin"] == "*"


def test_each_request_is_logged(client: TestClient, token, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    client.get(PATH, headers=bearer(token("a-ws", sub="u1")) | {"Range": "bytes=0-16383"})
    client.get(PATH)

    records = access_records(caplog)
    assert [(r["status"], r.get("workspace"), r["range"]) for r in records] == [
        (307, "a-ws", "bytes=0-16383"),
        (401, None, None),
    ]
    assert records[0]["sub"] == "u1"


def test_log_describes_the_authorization_header_without_the_token(
    client: TestClient, token, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    secret = token("a-ws")
    client.get(PATH, headers={"Authorization": secret})  # no "Bearer " prefix
    client.get(PATH, headers=bearer(secret))

    assert secret not in caplog.text
    records = access_records(caplog)
    assert records[0]["authorization"] == f"no scheme, {len(secret)} chars"
    assert records[1]["authorization"] == f"Bearer, {len(secret)} chars"


def test_ready_once_the_known_object_is_readable(client: TestClient, sts: FakeSTS) -> None:
    with client:  # runs the startup check
        assert client.get("/readyz").status_code == 200
    assert [c["RoleSessionName"] for c in sts.calls] == [READINESS_SESSION]


def test_not_ready_while_the_role_cannot_read_the_known_object(
    client: TestClient, s3_errors: dict[str, ClientError], caplog: pytest.LogCaptureFixture
) -> None:
    s3_errors[READINESS_KEY] = s3_error(403, "403")
    with client:
        assert client.get("/readyz").status_code == 503
        assert "Check the signer role's policy" in caplog.text

        del s3_errors[READINESS_KEY]  # the policy is fixed
        assert client.get("/readyz").status_code == 200


def test_readiness_session_cannot_be_a_workspace_name() -> None:
    # Workspace names are DNS labels, so the check is never charged to a workspace.
    assert "_" in READINESS_SESSION


def test_healthz(client: TestClient) -> None:
    assert client.get("/healthz").status_code == 200
