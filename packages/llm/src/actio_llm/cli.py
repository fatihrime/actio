"""Command-line entry point: ``actio-llm chat granite4:micro "Hello"``.

A way to try the Ollama adapter without the rest of Actio: it streams one answer to the
terminal and prints what the adapter learned about it (tokens, speed, load time).
"""

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Sequence
from contextlib import aclosing

import httpx2

from actio_llm.chat import (
    ChatRequest,
    Finish,
    Message,
    ReasoningDelta,
    System,
    TextDelta,
    ToolCall,
    User,
)
from actio_llm.errors import LLMError
from actio_llm.ollama import Ollama

DEFAULT_URL = "http://127.0.0.1:11434"


def make_client(url: str) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(base_url=url)


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="actio-llm", description="Talk to a local model through Actio's Ollama adapter."
    )
    parser.add_argument(
        "--url",
        default=os.environ.get("ACTIO_OLLAMA_URL", DEFAULT_URL),
        help="the Ollama server (default: $ACTIO_OLLAMA_URL, or %(default)s)",
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    commands.add_parser("models", help="list the server's models and what each can do")
    chat = commands.add_parser("chat", help="send one prompt and stream the answer")
    chat.add_argument("model", help="a model on the server, such as granite4:micro")
    chat.add_argument("prompt")
    chat.add_argument("--system", metavar="TEXT", help="instructions that come before the prompt")
    chat.add_argument(
        "--context",
        type=_positive,
        metavar="TOKENS",
        help="the context window to load the model with (default: the server's)",
    )
    chat.add_argument(
        "--max-output", type=_positive, metavar="TOKENS", help="cut the answer after this many"
    )
    chat.add_argument(
        "--think",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="turn the model's reasoning on or off (default: the model's own choice)",
    )
    args = parser.parse_args(argv)

    try:
        asyncio.run(_run(args))
    except LLMError as error:
        sys.stderr.write(f"actio-llm: {error}\n")
        raise SystemExit(1) from error


def _positive(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return value


async def _run(args: argparse.Namespace) -> None:
    async with make_client(args.url) as client:
        ollama = Ollama(client)
        if args.command == "models":
            await _models(ollama)
        else:
            await _chat(ollama, args)


async def _models(ollama: Ollama) -> None:
    models = await ollama.models()
    if not models:
        sys.stdout.write("No models yet. Pull one with: ollama pull granite4:micro\n")
        return
    for model in models:
        info = await ollama.describe(model.name)
        context = f"{info.context_length:,}" if info.context_length else "?"
        sys.stdout.write(
            f"{model.name}  {model.size_bytes / 1e9:.1f} GB  "
            f"{model.parameter_size or '?'} {model.quantization or ''}  "
            f"context up to {context}  [{', '.join(sorted(info.capabilities))}]\n"
        )


async def _chat(ollama: Ollama, args: argparse.Namespace) -> None:
    messages: list[Message] = [System(args.system)] if args.system else []
    messages.append(User(args.prompt))
    request = ChatRequest(
        model=args.model,
        messages=messages,
        context_window=args.context,
        max_output_tokens=args.max_output,
        thinking=args.think,
    )
    out = sys.stdout
    dim, plain = ("\033[2m", "\033[0m") if out.isatty() else ("", "")
    reasoning = False
    async with aclosing(ollama.stream(request)) as events:
        async for event in events:
            match event:
                case ReasoningDelta(text=text):
                    out.write(f"{dim}{text}{plain}")
                    reasoning = True
                case TextDelta(text=text):
                    if reasoning:  # set the answer apart from the thinking before it
                        out.write("\n\n")
                        reasoning = False
                    out.write(text)
                case ToolCall(name=name, arguments=arguments):
                    out.write(f"[tool call] {name}({json.dumps(dict(arguments))})")
                case Finish():
                    out.write("\n")
                    out.flush()  # so the answer is on screen before the statistics
                    sys.stderr.write(_stats(event) + "\n")
            out.flush()


def _stats(finish: Finish) -> str:
    usage = finish.usage
    parts = [
        f"[{finish.reason}]",
        f"prompt {usage.input_tokens} tokens ({usage.cached_input_tokens} from cache)",
        f"answer {usage.output_tokens} tokens",
    ]
    if (speed := finish.tokens_per_second) is not None:
        parts.append(f"{speed:.1f} tokens/s")
    if finish.timings is not None:
        parts.append(f"model load {finish.timings.load_ms / 1000:.1f} s")
    return " · ".join(parts)
