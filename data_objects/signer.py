"""Presigned S3 URLs, signed by a role session named after the workspace.

The session name is what makes the cost attributable: it appears in every CloudTrail data
event as `userIdentity.arn = .../<signer-role>/<workspace>`.

Sessions are cached per workspace and region, and replaced while at least the refresh margin
remains. A presigned URL stops working when the session that signed it expires, so Settings
requires the margin to be at least the URL lifetime: every URL is valid for its full lifetime.
"""

import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from typing import Any
from urllib.parse import urlencode, urlsplit

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from data_objects.settings import Bucket

# Response fields from HeadObject that the endpoint passes on when it answers a HEAD itself.
HEAD_HEADERS = {
    "ContentLength": "Content-Length",
    "ContentType": "Content-Type",
    "ETag": "ETag",
    "LastModified": "Last-Modified",
}

# S3 statuses from HeadObject that mean "no such object" to the client. Without s3:ListBucket,
# which the Hub cannot get on a bucket it does not own, S3 answers a missing key with 403
# (ADR-003, decision 4). The caller logs the original status, so a broken role policy is
# visible in the logs even though the client sees 404.
MISSING_STATUSES = frozenset({403, 404})


@dataclass
class _Session:
    s3: Any
    expires: datetime


@dataclass(frozen=True)
class ObjectHead:
    headers: dict[str, str]


@dataclass(frozen=True)
class Missing:
    """HeadObject found no object, or was refused. `s3_status` and `s3_code` are for the log."""

    s3_status: int
    s3_code: str


class Signer:
    def __init__(
        self,
        role_arn: str,
        *,
        url_lifetime: timedelta,
        session_duration: timedelta,
        refresh_margin: timedelta,
        sts: Any = None,
        now: Callable[[], datetime] = lambda: datetime.now(tz=UTC),
        s3_factory: Callable[[dict[str, Any], str], Any] | None = None,
    ) -> None:
        self._role_arn = role_arn
        self._url_lifetime = url_lifetime
        self._session_duration = session_duration
        self._refresh_margin = refresh_margin
        self._sts = sts or boto3.client("sts")
        self._now = now
        self._s3_factory = s3_factory or _s3_client
        self._sessions: dict[tuple[str, str], _Session] = {}
        self._lock = threading.Lock()

    def presign(self, *, workspace: str, bucket: Bucket, key: str) -> str:
        """A presigned GET URL. Only GET is ever presigned: see `head` for why."""
        params: dict[str, str] = {"Bucket": bucket.name, "Key": key}
        if bucket.requester_pays:
            params["RequestPayer"] = "requester"
        s3 = self._session(workspace, bucket.region).s3
        return s3.generate_presigned_url("get_object", Params=params, ExpiresIn=int(self._url_lifetime.total_seconds()))

    def head(self, *, workspace: str, bucket: Bucket, key: str) -> ObjectHead | Missing:
        """HeadObject as the workspace's session, for the endpoint to answer HEAD itself.

        A presigned URL covers one method, and GDAL caches the target of its first redirect
        for later range GETs. If HEAD were redirected to a HEAD-signed URL, GDAL would send
        its GETs to that URL and S3 would refuse them. Answering HEAD here means the first
        redirect GDAL sees is for GET. The call is made as the workspace's session, so
        CloudTrail still attributes it.

        Raises ClientError for any S3 failure other than those in MISSING_STATUSES.
        """
        params: dict[str, str] = {"Bucket": bucket.name, "Key": key}
        if bucket.requester_pays:
            params["RequestPayer"] = "requester"
        s3 = self._session(workspace, bucket.region).s3
        try:
            response = s3.head_object(**params)
        except ClientError as e:
            status = e.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if status in MISSING_STATUSES:
                return Missing(s3_status=status, s3_code=str(e.response.get("Error", {}).get("Code", "")))
            raise

        headers = {"Accept-Ranges": "bytes"}
        for name, header in HEAD_HEADERS.items():
            if name in response:
                value = response[name]
                headers[header] = (
                    format_datetime(value.astimezone(UTC), usegmt=True) if isinstance(value, datetime) else str(value)
                )
        return ObjectHead(headers=headers)

    def _session(self, workspace: str, region: str) -> _Session:
        with self._lock:
            cached = self._sessions.get((workspace, region))
            if cached and cached.expires - self._now() > self._refresh_margin:
                return cached

            credentials = self._sts.assume_role(
                RoleArn=self._role_arn,
                RoleSessionName=workspace,
                DurationSeconds=int(self._session_duration.total_seconds()),
            )["Credentials"]
            session = _Session(s3=self._s3_factory(credentials, region), expires=credentials["Expiration"])
            self._sessions[(workspace, region)] = session
            return session


def _request_payer_in_query(request: Any, **_: Any) -> None:
    """Move `x-amz-request-payer` from the headers into the query string before signing.

    boto3 signs RequestPayer as a header (`X-Amz-SignedHeaders=host;x-amz-request-payer`). A
    client that follows the redirect does not know to send that header, so S3 would reject
    the signature. S3 also accepts the value as a query parameter, which SigV4 query signing
    covers like any other, so the URL works on its own.

    Presigned URLs only. On an ordinary header-signed call (the endpoint's own HeadObject),
    S3 ignores the query-string form and refuses the request with 403, as if the caller had
    not agreed to pay.
    """
    if not request.context.get("is_presign_request"):
        return
    payer = request.headers.get("x-amz-request-payer")
    if payer is None:
        return
    # botocore's HTTPHeaders is an email.message.Message: no pop(), and del removes every copy.
    del request.headers["x-amz-request-payer"]
    separator = "&" if urlsplit(request.url).query else "?"
    request.url = f"{request.url}{separator}{urlencode({'x-amz-request-payer': payer})}"


def _s3_client(credentials: dict[str, Any], region: str) -> Any:
    client = boto3.client(
        "s3",
        region_name=region,
        aws_access_key_id=credentials["AccessKeyId"],
        aws_secret_access_key=credentials["SecretAccessKey"],
        aws_session_token=credentials["SessionToken"],
        # SigV4 and a regional virtual-hosted endpoint: no redirect from the global endpoint,
        # which would add a hop and break the signature for some clients.
        config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
        endpoint_url=f"https://s3.{region}.amazonaws.com",
    )
    client.meta.events.register("before-sign.s3.*", _request_payer_in_query)
    return client
