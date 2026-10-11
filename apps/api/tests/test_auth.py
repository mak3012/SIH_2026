"""Token verification and the mapping of Keycloak roles to ABAC attributes."""

import time
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from starlette.requests import Request

from api import auth

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
ISS = "http://localhost:8080/realms/wells"


class FakeJwks:
    def get_signing_key_from_jwt(self, token: str) -> Any:
        return type("K", (), {"key": KEY.public_key()})()


def request(token: str | None = None, **headers: str) -> Request:
    h = [(k.lower().replace("_", "-").encode(), v.encode()) for k, v in headers.items()]
    if token:
        h.append((b"authorization", f"Bearer {token}".encode()))
    return Request({"type": "http", "headers": h})


def token(**over: Any) -> str:
    claims = {
        "iss": ISS,
        "aud": ["api", "account"],
        "exp": int(time.time()) + 300,
        "preferred_username": "engineer",
        "realm_access": {
            "roles": ["planner", "block:AA-ONN-2001/1", "jv:JV-DEMO", "default-roles-wells"]
        },
        **over,
    }
    return jwt.encode(claims, KEY, algorithm="RS256")


@pytest.fixture(autouse=True)
def env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OIDC_ISSUER_PUBLIC", ISS)
    monkeypatch.setenv("OIDC_ISSUER", "http://keycloak:8080/realms/wells")
    monkeypatch.delenv("DEV_AUTH", raising=False)
    monkeypatch.setattr(auth, "_jwks", lambda: FakeJwks())


def test_valid_token_maps_roles_to_blocks_and_jvs() -> None:
    u = auth.current_user(request(token()))
    assert u.name == "engineer" and u.blocks == ("AA-ONN-2001/1",) and u.jvs == ("JV-DEMO",)
    assert "planner" in u.roles and not any(":" in r for r in u.roles)
    u.require("plan")
    with pytest.raises(HTTPException) as e:
        u.require("admin")
    assert e.value.status_code == 403


@pytest.mark.parametrize(
    ("role", "allowed"),
    [
        ("planner", {"plan"}),
        ("wellsite_engineer", {"operate"}),
        ("superintendent", {"operate"}),
        ("data_steward", {"review"}),
        ("admin", {"admin"}),
        ("viewer", set()),
    ],
)
def test_each_role_has_only_its_permissions(role: str, allowed: set[str]) -> None:
    u = auth.User("u", frozenset({role}), ())
    assert {p for p in auth.PERMISSIONS if u.can(p)} == allowed


def test_self_registered_viewer_reads_its_blocks_and_acts_on_nothing() -> None:
    u = auth.current_user(
        request(token(realm_access={"roles": ["default-roles-wells", "viewer", "block:SA-BLK-1"]}))
    )
    assert u.blocks == ("SA-BLK-1",)
    with pytest.raises(HTTPException) as e:
        u.require("plan")
    assert e.value.status_code == 403


def test_valid_token_without_application_role_is_forbidden() -> None:
    with pytest.raises(HTTPException) as e:
        auth.current_user(request(token(realm_access={"roles": ["block:AA-ONN-2001/1"]})))
    assert e.value.status_code == 403


@pytest.mark.parametrize(
    "bad", [{"aud": "account"}, {"iss": "http://evil/realms/wells"}, {"exp": int(time.time()) - 10}]
)
def test_bad_tokens_are_refused(bad: dict[str, Any]) -> None:
    with pytest.raises(HTTPException) as e:
        auth.current_user(request(token(**bad)))
    assert e.value.status_code == 401


def test_no_token_and_dev_headers_off_by_default() -> None:
    with pytest.raises(HTTPException) as e:
        auth.current_user(request(None, X_User="mallory", X_Blocks="AA-ONN-2001/1"))
    assert e.value.status_code == 401


def test_dev_headers_only_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEV_AUTH", "1")
    u = auth.current_user(request(None, X_User="dev", X_Roles="planner", X_Blocks="B1,B2"))
    assert u.blocks == ("B1", "B2") and u.roles == frozenset({"planner"})
