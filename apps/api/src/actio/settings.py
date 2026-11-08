"""Process configuration, read from ``ACTIO_*`` environment variables.

Settings never read ``.env`` files themselves: whatever starts the process supplies
the environment. Tests build their own settings, so local secrets can't leak into a
test run.
"""

from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ACTIO_", frozen=True)

    host: str = "127.0.0.1"
    """Interface to listen on. Localhost by default; containers set ``0.0.0.0``."""

    port: int = Field(default=8000, ge=1, le=65535)

    log_level: Literal["critical", "error", "warning", "info", "debug"] = "info"
