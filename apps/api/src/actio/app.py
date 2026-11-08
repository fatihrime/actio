"""Builds the FastAPI application. ``uvicorn --factory`` calls :func:`create_app`."""

from fastapi import FastAPI

from actio import __version__, health


def create_app() -> FastAPI:
    app = FastAPI(
        title="Actio",
        version=__version__,
        # The reverse proxy sends /api/* to this service and everything else to the
        # web app, so the API docs live under /api too.
        openapi_url="/api/openapi.json",
        docs_url="/api/docs",
        redoc_url=None,
    )
    app.include_router(health.router)
    return app
