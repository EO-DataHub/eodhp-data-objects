from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from botocore.awsrequest import AWSRequest
from botocore.exceptions import ClientError

from data_objects.signer import Missing, ObjectHead, Signer, _request_payer_in_query
from tests.conftest import LANDSAT, OPEN_BUCKET, ROLE, Clock, FakeS3, FakeSTS, s3_error

KEY = "collection02/level-2/standard/oli-tirs/2024/048/022/X/X_MTL.json"


def presign(signer: Signer, workspace: str = "a-ws") -> str:
    return signer.presign(workspace=workspace, bucket=LANDSAT, key=KEY)


def test_url_is_regional_sigv4_and_requester_pays(signer: Signer) -> None:
    url = urlparse(presign(signer))
    query = parse_qs(url.query)

    assert url.scheme == "https"
    assert url.netloc == "usgs-landsat.s3.us-west-2.amazonaws.com"
    assert url.path == f"/{KEY}"
    assert query["X-Amz-Algorithm"] == ["AWS4-HMAC-SHA256"]
    assert query["x-amz-request-payer"] == ["requester"]
    # Signed as a query parameter, so the client does not have to send a header.
    assert query["X-Amz-SignedHeaders"] == ["host"]
    assert query["X-Amz-Expires"] == ["900"]
    assert query["X-Amz-Security-Token"] == ["token-1"]


def test_bucket_that_is_not_requester_pays_gets_no_payer(signer: Signer) -> None:
    url = urlparse(signer.presign(workspace="a-ws", bucket=OPEN_BUCKET, key=KEY))
    assert url.netloc == "hub-open-data.s3.eu-west-2.amazonaws.com"
    assert "x-amz-request-payer" not in parse_qs(url.query)


def test_session_is_named_after_the_workspace(signer: Signer, sts: FakeSTS) -> None:
    presign(signer, "a-ws")
    assert sts.calls == [{"RoleArn": ROLE, "RoleSessionName": "a-ws", "DurationSeconds": 3600}]


def test_session_is_reused_while_more_than_the_margin_remains(signer: Signer, sts: FakeSTS, clock: Clock) -> None:
    presign(signer)
    clock.now += timedelta(minutes=39)
    presign(signer)
    assert len(sts.calls) == 1


def test_session_is_replaced_once_the_margin_is_reached(signer: Signer, sts: FakeSTS, clock: Clock) -> None:
    presign(signer)
    clock.now += timedelta(minutes=40)
    presign(signer)
    assert len(sts.calls) == 2


def test_each_workspace_gets_its_own_session(signer: Signer, sts: FakeSTS) -> None:
    presign(signer, "a-ws")
    presign(signer, "b-ws")
    assert [c["RoleSessionName"] for c in sts.calls] == ["a-ws", "b-ws"]


def test_lifetime_and_session_come_from_settings(sts: FakeSTS, clock: Clock) -> None:
    signer = Signer(
        ROLE,
        url_lifetime=timedelta(minutes=5),
        session_duration=timedelta(hours=2),
        refresh_margin=timedelta(minutes=10),
        sts=sts,
        now=clock,
    )
    query = parse_qs(urlparse(presign(signer)).query)
    assert query["X-Amz-Expires"] == ["300"]
    assert sts.calls[0]["DurationSeconds"] == 7200


def test_head_passes_on_size_type_and_ranges(signer: Signer, s3_clients: list[FakeS3]) -> None:
    head = signer.head(workspace="a-ws", bucket=LANDSAT, key=KEY)

    assert head == ObjectHead(
        headers={
            "Accept-Ranges": "bytes",
            "Content-Length": "71155923",
            "Content-Type": "image/tiff",
            "ETag": '"abc"',
            "Last-Modified": "Mon, 02 Nov 2026 00:00:00 GMT",
        }
    )
    assert s3_clients[0].heads == [{"Bucket": "usgs-landsat", "Key": KEY, "RequestPayer": "requester"}]


def test_head_on_a_bucket_that_is_not_requester_pays_sends_no_payer(signer: Signer, s3_clients: list[FakeS3]) -> None:
    signer.head(workspace="a-ws", bucket=OPEN_BUCKET, key=KEY)
    assert s3_clients[0].heads == [{"Bucket": "hub-open-data", "Key": KEY}]


@pytest.mark.parametrize(("status", "code"), [(403, "403"), (404, "404")])
def test_head_refused_or_not_found_is_missing(
    signer: Signer, s3_errors: dict[str, ClientError], status: int, code: str
) -> None:
    s3_errors["x/gone"] = s3_error(status, code)
    assert signer.head(workspace="a-ws", bucket=LANDSAT, key="x/gone") == Missing(s3_status=status, s3_code=code)


def test_head_other_failures_raise(signer: Signer, s3_errors: dict[str, ClientError]) -> None:
    s3_errors["x/slow"] = s3_error(503, "SlowDown")
    with pytest.raises(ClientError):
        signer.head(workspace="a-ws", bucket=LANDSAT, key="x/slow")


def test_head_uses_the_workspace_session(signer: Signer, sts: FakeSTS) -> None:
    signer.head(workspace="b-ws", bucket=LANDSAT, key=KEY)
    assert [c["RoleSessionName"] for c in sts.calls] == ["b-ws"]


def test_payer_hook_leaves_ordinary_requests_alone() -> None:
    # A header-signed call with the payer moved into the query string gets 403 from S3.
    request = AWSRequest(
        method="HEAD", url="https://b.s3.us-west-2.amazonaws.com/k", headers={"x-amz-request-payer": "requester"}
    )
    _request_payer_in_query(request)
    assert request.headers["x-amz-request-payer"] == "requester"
    assert request.url == "https://b.s3.us-west-2.amazonaws.com/k"


def test_payer_hook_moves_the_payer_into_a_presigned_query() -> None:
    request = AWSRequest(
        method="GET", url="https://b.s3.us-west-2.amazonaws.com/k", headers={"x-amz-request-payer": "requester"}
    )
    request.context["is_presign_request"] = True
    _request_payer_in_query(request)
    assert "x-amz-request-payer" not in request.headers
    assert request.url == "https://b.s3.us-west-2.amazonaws.com/k?x-amz-request-payer=requester"
