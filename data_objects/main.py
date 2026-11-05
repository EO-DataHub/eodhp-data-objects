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

app = create_app(
    settings,
    signer=Signer(
        settings.signer_role_arn,
        url_lifetime=settings.url_lifetime,
        session_duration=settings.session_duration,
        refresh_margin=settings.refresh_margin,
    ),
    verifier=Verifier(
        issuer=settings.keycloak_issuer,
        audience=settings.jwt_audience,
        key_for=jwks_resolver(settings.keycloak_issuer),
    ),
)
