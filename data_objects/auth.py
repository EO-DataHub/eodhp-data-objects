"""Token verification and the choice of workspace to charge (ADR-003, decision 3).

Verification follows accounting-service's authz.py: RS256 only, `exp`, `iss` and `sub`
required, the platform audiences, a small leeway, and Keycloak's JWKS for the key.
"""

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import jwt
from jwt import PyJWKClient, PyJWKClientConnectionError, PyJWTError

JWT_LEEWAY = timedelta(seconds=10)

# How long a request waits on Keycloak for the JWKS document. PyJWKClient's default is 30 s,
# which would hold a worker thread for that long on every request while Keycloak is down.
JWKS_TIMEOUT_SECONDS = 5

# The workspace becomes the STS RoleSessionName, so it must fit that pattern. Real workspace
# names are DNS labels and fit easily. Anything else is refused before it reaches STS.
SESSION_NAME = re.compile(r"[\w+=,.@-]{2,64}")

# Takes the raw token and returns the key to verify it with.
KeyResolver = Callable[[str], Any]


class AuthError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


@dataclass(frozen=True)
class Caller:
    sub: str
    workspace: str


def jwks_resolver(issuer: str) -> KeyResolver:
    """Keys from the realm's JWKS endpoint. PyJWKClient caches them across requests, and
    building it does not contact Keycloak, so a Keycloak outage does not stop startup."""
    client = PyJWKClient(f"{issuer}/protocol/openid-connect/certs", timeout=JWKS_TIMEOUT_SECONDS)
    return lambda token: client.get_signing_key_from_jwt(token).key


def _claim_list(payload: dict[str, Any], claim: str) -> list[str]:
    """A single-element claim may arrive as a bare string. Never substring-match it."""
    value = payload.get(claim)
    if isinstance(value, str):
        return [value]
    if isinstance(value, list | tuple):
        return [item for item in value if isinstance(item, str)]
    return []


class Verifier:
    def __init__(self, *, issuer: str, audience: Sequence[str], key_for: KeyResolver) -> None:
        self._issuer = issuer
        self._audience = list(audience)
        self._key_for = key_for

    def decode(self, authorization: str | None) -> dict[str, Any]:
        if not authorization:
            raise AuthError(401, "No Authorization header")
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise AuthError(401, "The Authorization header is not 'Bearer <token>'")
        try:
            return jwt.decode(
                token,
                self._key_for(token),
                algorithms=["RS256"],
                audience=self._audience,
                issuer=self._issuer,
                leeway=JWT_LEEWAY,
                options={"require": ["exp", "iss", "sub"]},
            )
        except PyJWKClientConnectionError as e:
            # Keycloak is unreachable: the token may well be valid, so this is not a 401.
            raise AuthError(503, "Cannot fetch the keys to verify the token") from e
        except PyJWTError as e:
            raise AuthError(401, "Invalid token") from e


def choose_workspace(payload: dict[str, Any], header: str | None, query: str | None = None) -> str:
    """The workspace to charge: the header, else the `workspace` query parameter, else the
    token's only workspace.

    The query parameter is for clients that cannot send a header: a link click in the UI and
    a tile URL given to titiler. It is checked against the same claim as the header.

    400 when the choice is ambiguous or malformed, 403 when the token does not grant it.
    """
    workspaces = _claim_list(payload, "workspaces")
    named = header if header is not None else query

    if named is None:
        if len(workspaces) != 1:
            raise AuthError(400, "Send X-EODH-Workspace or ?workspace=: the token does not name exactly one workspace")
        workspace = workspaces[0]
    else:
        workspace = named.strip()
        if workspace not in workspaces:
            raise AuthError(403, "The token does not grant this workspace")

    if not SESSION_NAME.fullmatch(workspace):
        raise AuthError(400, "The workspace name cannot be used as a session name")

    return workspace


def authenticate(
    verifier: Verifier, authorization: str | None, workspace_header: str | None, workspace_query: str | None = None
) -> Caller:
    payload = verifier.decode(authorization)
    return Caller(sub=str(payload["sub"]), workspace=choose_workspace(payload, workspace_header, workspace_query))
