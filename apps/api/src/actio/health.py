"""Liveness probe. A readiness probe arrives with the database and Valkey."""

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from actio import __version__

router = APIRouter(tags=["health"])


class Health(BaseModel):
    status: Literal["ok"]
    version: str


@router.get("/healthz")
async def healthz() -> Health:
    """Answers whenever the process can serve requests."""
    return Health(status="ok", version=__version__)
