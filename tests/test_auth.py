import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt import PyJWKClientConnectionError

from data_objects.auth import AuthError, Verifier, authenticate, choose_workspace
from tests.conftest import ISSUER


def test_header_names_a_granted_workspace() -> None:
    assert choose_workspace({"workspaces": ["a-ws", "b-ws"]}, "b-ws") == "b-ws"


def test_query_names_a_granted_workspace() -> None:
    assert choose_workspace({"workspaces": ["a-ws", "b-ws"]}, None, "b-ws") == "b-ws"


def test_header_wins_over_query() -> None:
    assert choose_workspace({"workspaces": ["a-ws", "b-ws"]}, "a-ws", "b-ws") == "a-ws"


def test_single_workspace_is_the_default() -> None:
    assert choose_workspace({"workspaces": ["only-ws"]}, None) == "only-ws"


def test_bare_string_claim_counts_as_one_workspace() -> None:
    assert choose_workspace({"workspaces": "only-ws"}, None) == "only-ws"


@pytest.mark.parametrize("workspaces", [[], ["a-ws", "b-ws"]])
def test_no_header_and_no_single_workspace_is_400(workspaces: list[str]) -> None:
    with pytest.raises(AuthError) as e:
        choose_workspace({"workspaces": workspaces}, None)
    assert e.value.status == 400


@pytest.mark.parametrize(("header", "query"), [("other-ws", None), (None, "other-ws")])
def test_workspace_not_in_the_token_is_403(header: str | None, query: str | None) -> None:
    with pytest.raises(AuthError) as e:
        choose_workspace({"workspaces": ["a-ws"]}, header, query)
    assert e.value.status == 403


def test_bare_string_claim_is_not_substring_matched() -> None:
    with pytest.raises(AuthError) as e:
        choose_workspace({"workspaces": "abc-ws"}, "bc-ws")
    assert e.value.status == 403


def test_workspace_that_cannot_be_a_session_name_is_400() -> None:
    with pytest.raises(AuthError) as e:
        choose_workspace({"workspaces": ["bad name"]}, "bad name")
    assert e.value.status == 400


@pytest.mark.parametrize("authorization", [None, "", "Basic abc", "Bearer", "Bearer "])
def test_missing_or_malformed_authorization_is_401(verifier: Verifier, authorization: str | None) -> None:
    with pytest.raises(AuthError) as e:
        authenticate(verifier, authorization, None)
    assert e.value.status == 401


def test_token_from_another_key_is_401(token) -> None:
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048).public_key()
    verifier = Verifier(issuer=ISSUER, audience=["eodh"], key_for=lambda _t: other)
    with pytest.raises(AuthError) as e:
        authenticate(verifier, f"Bearer {token('a-ws')}", None)
    assert e.value.status == 401


@pytest.mark.parametrize(
    "claims",
    [
        {"hours": -1},
        {"iss": "https://elsewhere.example/keycloak/realms/eodhp"},
        {"aud": "some-other-client"},
    ],
    ids=["expired", "wrong issuer", "wrong audience"],
)
def test_token_that_fails_a_check_is_401(verifier: Verifier, token, claims: dict) -> None:
    with pytest.raises(AuthError) as e:
        authenticate(verifier, f"Bearer {token('a-ws', **claims)}", None)
    assert e.value.status == 401


def test_unsigned_token_is_401(verifier: Verifier) -> None:
    unsigned = jwt.encode({"sub": "x", "iss": ISSUER, "workspaces": ["a-ws"]}, None, algorithm="none")  # pyright: ignore[reportArgumentType]
    with pytest.raises(AuthError) as e:
        authenticate(verifier, f"Bearer {unsigned}", None)
    assert e.value.status == 401


def test_keycloak_unreachable_is_503(token) -> None:
    def unreachable(_token: str):
        raise PyJWKClientConnectionError("connection refused")

    verifier = Verifier(issuer=ISSUER, audience=["eodh"], key_for=unreachable)
    with pytest.raises(AuthError) as e:
        authenticate(verifier, f"Bearer {token('a-ws')}", None)
    assert e.value.status == 503


def test_valid_token_gives_the_caller(verifier: Verifier, token) -> None:
    caller = authenticate(verifier, f"bearer {token('a-ws', sub='u1')}", None)
    assert (caller.sub, caller.workspace) == ("u1", "a-ws")
