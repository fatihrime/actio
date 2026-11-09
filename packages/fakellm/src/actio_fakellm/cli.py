"""Command-line entry point: ``fakellm --port 8100``."""

import argparse
from collections.abc import Sequence

import uvicorn

from actio_fakellm.app import DEFAULT_MODELS, create_app


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="fakellm", description="Run the fake OpenAI-compatible LLM server."
    )
    parser.add_argument(
        "--host", default="127.0.0.1", help="address to bind (default: %(default)s)"
    )
    parser.add_argument("--port", type=int, default=8100, help="port (default: %(default)s)")
    parser.add_argument(
        "--model",
        dest="models",
        action="append",
        metavar="NAME",
        help=f"a model to serve; repeat for more (default: {', '.join(DEFAULT_MODELS)})",
    )
    args = parser.parse_args(argv)

    uvicorn.run(
        create_app(models=args.models or DEFAULT_MODELS),
        host=args.host,
        port=args.port,
        server_header=False,
    )
