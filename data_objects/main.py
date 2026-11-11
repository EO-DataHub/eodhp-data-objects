"""The ASGI app, built from the environment.

    uvicorn data_objects.main:app --host 0.0.0.0 --port 8000

Tests build the app with `create_app` instead, so importing `data_objects.app` needs no
environment or AWS credentials.
"""

import logging
import sys

from data_objects.app import create_app
from data_objects.auth import Verifier, jwks_resolver
from data_objects.settings import load_settings
from data_objects.signer import Signer

logging.basicConfig(stream=sys.stdout, level=logging.INFO, format="%(message)s")

settings = load_settings()

log = logging.getLogger("data_objects")


def _signer(role_arn: str) -> Signer:
    return Signer(
        role_arn,
        url_lifetime=settings.url_lifetime,
        session_duration=settings.session_duration,
        refresh_margin=settings.refresh_margin,
    )


if settings.thumbnails is None:
    log.info("THUMBNAIL_ROLE_ARN is not set: preview images need a token, like any other object")

app = create_app(
    settings,
    signer=_signer(settings.signer_role_arn),
    thumbnail_signer=_signer(settings.thumbnails.role_arn) if settings.thumbnails else None,
    verifier=Verifier(
        issuer=settings.keycloak_issuer,
        audience=settings.jwt_audience,
        key_for=jwks_resolver(settings.keycloak_issuer),
    ),
)
