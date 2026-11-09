"""GET /api/data/objects/{bucket}/{key} -> 307 to a presigned GET URL (ADR-003).

HEAD is answered by the endpoint itself, from a server-side HeadObject (see Signer.head).
"""

import json
import logging
import secrets
import threading
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from data_objects.auth import AuthError, Verifier, authenticate
from data_objects.settings import Settings
from data_objects.signer import Missing, Signer

ROUTE_PREFIX = "/api/data/objects"

# The role session name for the readiness check. Workspace names are DNS labels, which cannot
# contain "_", so this can never be charged to a workspace.
READINESS_SESSION = "_readiness-check"

access_log = logging.getLogger("data_objects.access")
log = logging.getLogger("data_objects")


def _describe_authorization(value: str | None) -> str | None:
    """The scheme and length only, never the token: enough to tell "no header" from "a header
    in the wrong form" when a client fails."""
    if value is None:
        return None
    scheme, _, credentials = value.partition(" ")
    if not credentials:
        return f"no scheme, {len(value)} chars"
    return f"{scheme}, {len(credentials)} chars"


def _access(record: dict[str, Any]) -> None:
    access_log.info(json.dumps(record, separators=(",", ":")))


class Readiness:
    """HeadObject on a known object, as the signer role (ADR-003, decision 4).

    A missing key and a broken role policy both give 403 without s3:ListBucket, so the
    endpoint cannot tell them apart on a request. This check can: the object is known to
    exist. Once it passes, it stays passed, so the probe costs one S3 request per pod.
    """

    def __init__(self, settings: Settings, signer: Signer) -> None:
        self._settings = settings
        self._signer = signer
        self._ready = False
        self._lock = threading.Lock()

    def check(self) -> bool:
        with self._lock:
            if self._ready:
                return True
            ref = self._settings.readiness_object
            try:
                result = self._signer.head(
                    workspace=READINESS_SESSION, bucket=self._settings.buckets[ref.bucket], key=ref.key
                )
            except (ClientError, BotoCoreError) as e:
                log.error("Readiness check failed for %s/%s: %s", ref.bucket, ref.key, e)
                return False
            if isinstance(result, Missing):
                log.error(
                    "Readiness check failed for %s/%s: S3 returned %s %s. Check the signer role's policy.",
                    ref.bucket,
                    ref.key,
                    result.s3_status,
                    result.s3_code,
                )
                return False
            self._ready = True
            log.info("Readiness check passed for %s/%s", ref.bucket, ref.key)
            return True


def create_app(settings: Settings, *, signer: Signer, verifier: Verifier) -> FastAPI:
    readiness = Readiness(settings, signer)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncGenerator[None]:
        # Run once at startup so a broken role shows in the log at once. A failure does not
        # stop the process: /readyz keeps retrying, in case the cause was transient.
        readiness.check()
        yield

    app = FastAPI(title="EODH data objects", lifespan=lifespan)

    # Responses to browsers need CORS headers, or the browser stops before following the
    # redirect. Browser JavaScript still cannot read usgs-landsat itself, which has no CORS.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "HEAD", "OPTIONS"],
        allow_headers=["Authorization", "X-EODH-Workspace", "Range"],
        expose_headers=["Location", "X-Request-Id"],
    )

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> Response:
        return Response(status_code=200)

    @app.get("/readyz", include_in_schema=False)
    def readyz() -> Response:
        return Response(status_code=200 if readiness.check() else 503)

    @app.api_route(f"{ROUTE_PREFIX}/{{bucket}}/{{key:path}}", methods=["GET", "HEAD"])
    def read_object(bucket: str, key: str, request: Request) -> Response:
        request_id = secrets.token_hex(6)
        record: dict[str, Any] = {
            "time": datetime.now(tz=UTC).isoformat(),
            "id": request_id,
            "method": request.method,
            "bucket": bucket,
            "key": key,
            "range": request.headers.get("range"),
            "workspace_header": request.headers.get("x-eodh-workspace"),
            "workspace_query": request.query_params.get("workspace"),
            "user_agent": request.headers.get("user-agent"),
            "client": request.client.host if request.client else None,
            "authorization": _describe_authorization(request.headers.get("authorization")),
        }

        # The allowlist is checked before the token, and the S3 target is built from it, so
        # the endpoint can never redirect anywhere else.
        allowed = settings.buckets.get(bucket)
        if allowed is None or not key:
            _access(record | {"status": 404})
            return JSONResponse({"detail": "Not found"}, status_code=404)

        try:
            caller = authenticate(
                verifier,
                request.headers.get("authorization"),
                request.headers.get("x-eodh-workspace"),
                request.query_params.get("workspace"),
            )
        except AuthError as e:
            _access(record | {"status": e.status, "error": e.detail})
            return JSONResponse({"detail": e.detail}, status_code=e.status)

        who = {"workspace": caller.workspace, "sub": caller.sub}

        if request.method == "HEAD":
            try:
                head = signer.head(workspace=caller.workspace, bucket=allowed, key=key)
            except (ClientError, BotoCoreError) as e:
                _access(record | who | {"status": 502, "error": str(e)[:200]})
                return Response(status_code=502, headers={"X-Request-Id": request_id})
            if isinstance(head, Missing):
                _access(record | who | {"status": 404, "s3_status": head.s3_status, "s3_code": head.s3_code})
                return Response(status_code=404, headers={"X-Request-Id": request_id})
            _access(record | who | {"status": 200})
            return Response(status_code=200, headers=head.headers | {"X-Request-Id": request_id})

        try:
            url = signer.presign(workspace=caller.workspace, bucket=allowed, key=key)
        except (ClientError, BotoCoreError) as e:
            # Signing is local, but the first request for a workspace calls STS AssumeRole.
            _access(record | who | {"status": 502, "error": str(e)[:200]})
            return JSONResponse({"detail": "Cannot sign the request"}, status_code=502)
        _access(record | who | {"status": 307})
        return Response(
            status_code=307,
            headers={"Location": url, "Cache-Control": "no-store", "X-Request-Id": request_id},
        )

    return app
