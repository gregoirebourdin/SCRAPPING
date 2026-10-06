"""Request authentication (service JWT from the Next.js BFF) and workspace authorization."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

import jwt
import sqlalchemy as sa
from fastapi import Depends, Header, Request

from scout.config import get_settings
from scout.db.engine import session_scope
from scout.db.enums import MemberRole
from scout.db.models import User, Workspace, WorkspaceMember
from scout.errors import Forbidden, Unauthorized

_ROLE_RANK = {MemberRole.member: 0, MemberRole.admin: 1, MemberRole.owner: 2}


@dataclass(frozen=True)
class Principal:
    user_id: str
    email: str | None
    name: str | None


@dataclass(frozen=True)
class WorkspaceContext:
    workspace_id: uuid.UUID
    user_id: str
    role: MemberRole

    def require(self, role: MemberRole) -> None:
        if _ROLE_RANK[self.role] < _ROLE_RANK[role]:
            raise Forbidden(f"This action requires the {role.value} role")


def decode_service_token(token: str) -> Principal:
    s = get_settings()
    try:
        claims = jwt.decode(
            token,
            s.internal_api_secret.get_secret_value(),
            algorithms=["HS256"],
            audience=s.internal_jwt_audience,
            issuer=s.internal_jwt_issuer,
            options={"require": ["exp", "iat", "sub"]},
            leeway=5,
        )
    except jwt.PyJWTError as exc:
        raise Unauthorized(f"Invalid service token: {exc}") from exc
    return Principal(user_id=str(claims["sub"]), email=claims.get("email"), name=claims.get("name"))


def issue_service_token(
    user_id: str, *, email: str | None = None, name: str | None = None, ttl_s: int = 60
) -> str:
    """Used by tests/CLI. In production the Next.js BFF signs the same claims."""
    import time

    s = get_settings()
    now = int(time.time())
    payload = {
        "sub": user_id,
        "email": email,
        "name": name,
        "aud": s.internal_jwt_audience,
        "iss": s.internal_jwt_issuer,
        "iat": now,
        "exp": now + ttl_s,
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(payload, s.internal_api_secret.get_secret_value(), algorithm="HS256")


async def get_principal(request: Request, authorization: str | None = Header(default=None)) -> Principal:
    token = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise Unauthorized("Missing bearer token")
    principal = decode_service_token(token)
    request.state.principal = principal
    return principal


def _slugify(s: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return slug[:40] or "workspace"


async def ensure_user_and_workspace(principal: Principal) -> list[tuple[Workspace, MemberRole]]:
    """Bootstrap: make sure the user row exists (Better Auth creates it) and owns at least one workspace."""
    async with session_scope() as s:
        user = await s.get(User, principal.user_id)
        if user is None:
            if not principal.email:
                raise Unauthorized("Unknown user")
            user = User(id=principal.user_id, email=principal.email, name=principal.name or principal.email)
            s.add(user)
            await s.flush()
        rows = (
            await s.execute(
                sa.select(Workspace, WorkspaceMember.role)
                .join(WorkspaceMember, WorkspaceMember.workspace_id == Workspace.id)
                .where(WorkspaceMember.user_id == principal.user_id)
                .order_by(Workspace.created_at)
            )
        ).all()
        if rows:
            return [(w, r) for w, r in rows]
        base = (principal.name or (principal.email or "my").split("@")[0]).strip() or "My"
        ws = Workspace(
            name=f"{base.split()[0]}'s workspace",
            slug=f"{_slugify(base)}-{uuid.uuid4().hex[:6]}",
            monthly_budget_usd=get_settings().default_monthly_budget_usd,
        )
        s.add(ws)
        await s.flush()
        s.add(WorkspaceMember(workspace_id=ws.id, user_id=principal.user_id, role=MemberRole.owner))
        return [(ws, MemberRole.owner)]


async def get_workspace_context(
    principal: Principal = Depends(get_principal),
    x_workspace_id: str | None = Header(default=None),
) -> WorkspaceContext:
    workspace_id: uuid.UUID | None = None
    if x_workspace_id:
        try:
            workspace_id = uuid.UUID(x_workspace_id)
        except ValueError as exc:
            raise Forbidden("Invalid workspace id") from exc
    if workspace_id is not None:
        async with session_scope() as s:
            role = await s.scalar(
                sa.select(WorkspaceMember.role).where(
                    WorkspaceMember.workspace_id == workspace_id, WorkspaceMember.user_id == principal.user_id
                )
            )
        if role is not None:
            return WorkspaceContext(workspace_id=workspace_id, user_id=principal.user_id, role=role)
        # stale cookie: fall through to default workspace (never to someone else's)
    memberships = await ensure_user_and_workspace(principal)
    ws, role = memberships[0]
    return WorkspaceContext(workspace_id=ws.id, user_id=principal.user_id, role=role)
