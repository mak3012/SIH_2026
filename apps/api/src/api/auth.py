"""Who is calling: Keycloak access tokens -> user, roles, and the ABAC attributes (blocks, JVs)
that row-level security checks (api.db.scoped).

Tokens are verified against Keycloak's signing keys (RS256), expected issuer and audience. The
realm grants licences as realm roles `block:<id>` and `jv:<id>`, which Keycloak puts in every
access token; an audience mapper on the `web` client adds `api` to `aud`.

Roles (realm roles) grant permissions; see PERMISSIONS. Admin runs the system but does not
decide on data: no role can both edit facts and approve them, and none can do everything.

Development only: with DEV_AUTH=1 the headers X-User, X-Roles, X-Blocks and X-JVs stand in for a
token. It is refused unless explicitly enabled, and every such request is logged.
"""

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import cache
from typing import Any

import jwt
from fastapi import Depends, HTTPException, Request, status

log = logging.getLogger(__name__)

# Permission -> roles that hold it. Reads need any application role; RLS then limits the rows
# to the caller's blocks and JVs.
PERMISSIONS: dict[str, frozenset[str]] = {
    # offsets, programs, lint runs, briefings
    "plan": frozenset({"planner"}),
    # acknowledge, shelve, alert feedback, voice notes
    "operate": frozenset({"wellsite_engineer", "superintendent"}),
    # accept / correct / reject extracted values
    "review": frozenset({"data_steward"}),
    # system status, detector bank, LLM configuration, audit verification
    "admin": frozenset({"admin"}),
    # Rig Edge Pack service account: upload rig records, download hazard packages
    "sync": frozenset({"edge_sync"}),
}
# `viewer` holds no permission: it reads (within its blocks) and nothing else. Self-registered
# users start with it.
APP_ROLES = frozenset().union(*PERMISSIONS.values()) | {"viewer"}


@dataclass(frozen=True)
class User:
    name: str
    roles: frozenset[str]
    blocks: tuple[str, ...]
    jvs: tuple[str, ...] = field(default=())

    def can(self, permission: str) -> bool:
        return bool(self.roles & PERMISSIONS[permission])

    def require(self, permission: str) -> None:
        if not self.can(permission):
            roles = sorted(PERMISSIONS[permission])
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"{permission} needs one of {roles}")


def _list(v: Any) -> tuple[str, ...]:
    if v is None:
        return ()
    items = v if isinstance(v, list) else str(v).split(",")
    return tuple(sorted({str(x).strip() for x in items if str(x).strip()}))


@cache
def _jwks() -> jwt.PyJWKClient:
    url = os.environ.get("OIDC_JWKS_URL") or (
        os.environ["OIDC_ISSUER"].rstrip("/") + "/protocol/openid-connect/certs"
    )
    return jwt.PyJWKClient(url, cache_keys=True)


def from_claims(claims: dict[str, Any]) -> User:
    """Roles from realm_access.roles. Blocks and JVs from realm roles `block:<id>` / `jv:<id>`
    (always in the token, no mapper needed) and from optional `blocks` / `jvs` claims."""
    roles = frozenset((claims.get("realm_access") or {}).get("roles", []))
    blocks = {r.removeprefix("block:") for r in roles if r.startswith("block:")}
    jvs = {r.removeprefix("jv:") for r in roles if r.startswith("jv:")}
    return User(
        str(claims.get("preferred_username") or claims.get("sub")),
        frozenset(r for r in roles if ":" not in r),
        tuple(sorted(blocks | set(_list(claims.get("blocks"))))),
        tuple(sorted(jvs | set(_list(claims.get("jvs"))))),
    )


def current_user(request: Request) -> User:
    return authenticate(request.headers)


def authenticate(headers: Mapping[str, str]) -> User:
    """User from request (or WebSocket handshake) headers; raises 401, or 403 for a valid
    identity without any application role."""
    user = _identify(headers)
    if not user.roles & APP_ROLES:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no application role")
    return user


def _identify(headers: Mapping[str, str]) -> User:
    if os.environ.get("DEV_AUTH") == "1":
        u = headers.get("X-User")
        if u:
            log.warning("DEV_AUTH request as %s", u)
            return User(
                u,
                frozenset(_list(headers.get("X-Roles"))),
                _list(headers.get("X-Blocks")),
                _list(headers.get("X-JVs")),
            )
    header = headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "bearer token required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = header.removeprefix("Bearer ")
    try:
        key = _jwks().get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            key.key,
            algorithms=["RS256"],
            audience=os.environ.get("OIDC_AUDIENCE", "api"),
            issuer=os.environ.get("OIDC_ISSUER_PUBLIC", os.environ.get("OIDC_ISSUER")),
        )
    except (jwt.PyJWTError, KeyError) as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"invalid token: {e}") from e
    return from_claims(claims)


CurrentUser = Depends(current_user)
