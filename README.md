# eodhp-data-objects

Signed access to objects in allowlisted S3 buckets, charged to a workspace. The first use is Landsat Collection 2 in the requester-pays bucket `usgs-landsat`.

A client requests `/api/data/objects/{bucket}/{key}` with a bearer token. The service chooses the workspace to charge, signs a short-lived S3 URL as a role session named after that workspace, and redirects the client to it. CloudTrail S3 data events then record each read, and its bytes, under the workspace's name.

The contract is [ADR-003: URL contract for requester-pays data access](https://github.com/EO-DataHub/eodhp-guide/blob/main/docs/explanation/design-decisions/requester-pays-url-contract-adr.md). The background and proof of concept are in the [feasibility study](https://github.com/EO-DataHub/eodhp-guide/blob/main/docs/explanation/design-decisions/landsat-requester-pays-feasibility.md).

## Behaviour

| Request | Response |
|---|---|
| `GET` | `307` to a presigned S3 GET URL, with `Cache-Control: no-store`. The service does not check that the object exists. |
| `HEAD` | Answered by the service from `HeadObject`: `200` with `Content-Length`, `Content-Type`, `ETag`, `Last-Modified` and `Accept-Ranges`. It is never redirected, because GDAL reuses the target of its first redirect for its range GETs. |

The workspace is the first of these that is present: the `X-EODH-Workspace` header, the `workspace` query parameter, or the token's only workspace. A blank header or parameter counts as missing. It must be in the token's `workspaces` claim.

| Status | When |
|---|---|
| `400` | No workspace could be chosen, or the name cannot be an STS session name |
| `401` | No token, or the token is not valid |
| `403` | The chosen workspace is not in the token |
| `404` | The bucket is not on the allowlist (checked before the token), or `HeadObject` returned `403` or `404` |
| `502` | `HeadObject` or STS failed for another reason |
| `503` | Keycloak's keys could not be fetched |

A `403` from `HeadObject` becomes `404`, because without `s3:ListBucket` S3 answers a missing key with `403`. The access log keeps the original S3 status.

Each request writes one JSON line to the `data_objects.access` log on stdout. The line records the chosen workspace, the token's `sub`, the key and the `Range` header. It records the `Authorization` header's scheme and length, never the token.

### Preview images

When `THUMBNAIL_ROLE_ARN` is set, catalogue preview images need no token (ADR-003, decision 6). A preview is a key that ends with one of its bucket's `thumbnail_suffixes` in `BUCKETS`. For Landsat they are `_thumb_small.jpeg` and `_thumb_large.jpeg`. A key with the same ending in a bucket without `thumbnail_suffixes` is data, and needs a token.

- The service signs previews as the thumbnail role, with the session name `catalogue-thumbnails`. That role's IAM policy should allow only preview keys, so a bug in the service cannot sign a read of data.
- The Hub pays, and no workspace is charged. A token sent with a preview is ignored, even one that is not valid.
- A GET returns `307` with `Cache-Control: max-age=<THUMBNAIL_CACHE_SECONDS>`, so a browser reuses the redirect instead of asking again for each page view.
- HEAD is answered from `HeadObject`, as for other objects.
- The access log line has `"thumbnail": true` and no workspace.

The service does not limit how often a preview is requested. The ingress that serves previews without a login must do that.

Without `THUMBNAIL_ROLE_ARN`, and with no bucket that has `thumbnail_suffixes`, every object needs a token.

## Configuration

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `KEYCLOAK_BASE_URL` | yes | | The platform URL, for example `https://hub.example`. The issuer is `<base>/keycloak/realms/<realm>`. |
| `KEYCLOAK_REALM` | no | `eodhp` | |
| `SIGNER_ROLE_ARN` | yes | | The role the service assumes, once per workspace. It needs `s3:GetObject` on each allowlisted bucket. |
| `BUCKETS` | yes | | JSON list, for example `[{"name": "usgs-landsat", "region": "us-west-2", "requester_pays": true, "thumbnail_suffixes": ["_thumb_small.jpeg", "_thumb_large.jpeg"]}]`. `requester_pays` must be `true` or `false`, not a string. `thumbnail_suffixes` is optional: the key endings that mark a preview image in that bucket. They must match the thumbnail role's IAM policy for the bucket. |
| `READINESS_OBJECT` | yes | | `<bucket>/<key>` of an object that exists. See [Health checks](#health-checks). |
| `URL_LIFETIME_SECONDS` | no | `900` | How long a presigned URL is valid |
| `SESSION_DURATION_SECONDS` | no | `3600` | How long each workspace's STS session lasts. At least 900, and at most the role's maximum session duration. |
| `SESSION_REFRESH_MARGIN_SECONDS` | no | `1200` | A session is replaced when less than this remains |
| `THUMBNAIL_ROLE_ARN` | with `thumbnail_suffixes` | | The role that signs preview images. Setting it makes previews anonymous. The service refuses to start if it is set and no bucket has `thumbnail_suffixes`, or if a bucket has them and it is not set. |
| `THUMBNAIL_READINESS_OBJECT` | with `THUMBNAIL_ROLE_ARN` | | `<bucket>/<key>` of a preview image that exists. It must be a preview by the same rule as requests: in a bucket with `thumbnail_suffixes`, and ending with one of them. |
| `THUMBNAIL_CACHE_SECONDS` | no | `300` | `max-age` on a preview redirect. Must be shorter than `URL_LIFETIME_SECONDS`. |

The service refuses to start unless `URL_LIFETIME_SECONDS` ≤ `SESSION_REFRESH_MARGIN_SECONDS` < `SESSION_DURATION_SECONDS`, and `SESSION_DURATION_SECONDS` is between 15 minutes and 12 hours (the limits STS sets). The first rule makes sure every URL is valid for its full lifetime.

## Health checks

- `/healthz` returns `200` while the process runs.
- `/readyz` returns `200` once a `HeadObject` on `READINESS_OBJECT` as the signer role, and on `THUMBNAIL_READINESS_OBJECT` as the thumbnail role if it is set, have succeeded, and `503` until then. A missing key and a broken role policy both give `403` on a request, so this check is how a broken policy shows. It runs at startup and again on each probe until it passes. Its role session is named `_readiness-check`, which cannot be a workspace name.

## Development

```bash
uv sync
make check        # ruff, format check, pyright, pytest
```

To run it locally against AWS, set the variables above and run:

```bash
uv run uvicorn data_objects.main:app --port 8000
```

The tests need no AWS access or Keycloak: they use a fake STS, boto3's local presigning, and tokens signed with a key made for the test run.
