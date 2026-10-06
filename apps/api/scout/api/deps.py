"""Shared FastAPI dependencies."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends

from scout.auth.context import Principal, WorkspaceContext, get_principal, get_workspace_context

Ctx = Annotated[WorkspaceContext, Depends(get_workspace_context)]
Who = Annotated[Principal, Depends(get_principal)]
