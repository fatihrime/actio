"""Process configuration, read from ``ACTIO_*`` environment variables.

Settings never read ``.env`` files themselves: whatever starts the process supplies the
environment. Tests build their own settings, so local secrets can't leak into a test
run.
"""

from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ACTIO_", frozen=True)

    host: str = "127.0.0.1"
    """Interface to listen on. Localhost by default; containers set ``0.0.0.0``."""

    port: int = Field(default=8000, ge=1, le=65535)

    log_level: Literal["critical", "error", "warning", "info", "debug"] = "info"

    forwarded_allow_ips: str = "127.0.0.1"
    """Proxies trusted to report the client's address and scheme (``X-Forwarded-*``).

    Comma-separated addresses, or ``*`` for any. ``compose.yaml`` sets ``*``, because in
    that stack only the reverse proxy and the web app can reach the API.
    """

    database_url: SecretStr
    """Postgres, as ``postgresql://user:password@host:5432/database``. Required.

    It holds a password, so it is a secret: it never appears when settings are printed.
    A password with characters such as ``@`` or ``/`` must be percent-encoded.
    """

    valkey_url: SecretStr
    """Valkey, as ``valkey://:password@host:6379/0``. Required, and a secret too."""

    public_url: str = "http://localhost:8080"
    """The address people open in their browser, such as ``https://chat.example.com``.

    It decides two things. Requests that change something must come from pages at this
    origin. And when it is ``https``, the session cookie is marked ``Secure`` and takes
    the ``__Host-`` prefix; over plain ``http`` it can't be.
    """

    @field_validator("database_url")
    @classmethod
    def _is_postgres(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().startswith(("postgresql://", "postgres://")):
            raise ValueError("must start with postgresql://")
        return value

    @field_validator("public_url")
    @classmethod
    def _is_an_origin(cls, value: str) -> str:
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ValueError("must look like https://chat.example.com")
        if parts.path not in ("", "/") or parts.query or parts.fragment:
            raise ValueError("must be an address only, with no path")
        return f"{parts.scheme}://{parts.netloc}"

    @field_validator("valkey_url")
    @classmethod
    def _is_valkey(cls, value: SecretStr) -> SecretStr:
        schemes = ("valkey://", "valkeys://", "redis://", "rediss://")
        if not value.get_secret_value().startswith(schemes):
            raise ValueError("must start with valkey:// (or valkeys://, for TLS)")
        return value
