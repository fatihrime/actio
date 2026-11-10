"""The official OpenAI SDK reads fakellm's responses: proof that the wire format is right."""

import json
from collections.abc import AsyncIterator

import httpx2
import pytest
from fastapi import FastAPI
from openai import AsyncOpenAI, NotFoundError, RateLimitError
from openai.types import CompletionUsage
from openai.types.chat import ChatCompletionMessageParam

pytestmark = pytest.mark.anyio

HELLO: list[ChatCompletionMessageParam] = [{"role": "user", "content": "hello there"}]


@pytest.fixture
async def sdk(app: FastAPI) -> AsyncIterator[AsyncOpenAI]:
    """The SDK's async client, sending its requests straight to the app."""
    http_client = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app))
    async with AsyncOpenAI(
        api_key="unused", base_url="http://fakellm/v1", http_client=http_client, max_retries=0
    ) as client:
        yield client


async def test_sdk_reads_a_completion(sdk: AsyncOpenAI) -> None:
    completion = await sdk.chat.completions.create(model="fake-chat", messages=HELLO)

    assert completion.choices[0].message.content == "Echo: hello there"
    assert completion.usage is not None
    assert completion.usage.total_tokens == 5


async def test_sdk_reads_a_stream_with_usage(sdk: AsyncOpenAI) -> None:
    stream = await sdk.chat.completions.create(
        model="fake-chat", messages=HELLO, stream=True, stream_options={"include_usage": True}
    )
    text = ""
    usage: CompletionUsage | None = None
    async for chunk in stream:
        if chunk.choices:
            text += chunk.choices[0].delta.content or ""
        if chunk.usage is not None:
            usage = chunk.usage

    assert text == "Echo: hello there"
    assert usage is not None
    assert usage.completion_tokens == 3


async def test_sdk_lists_the_models(sdk: AsyncOpenAI) -> None:
    assert [model.id async for model in sdk.models.list()] == ["fake-chat"]


async def test_sdk_raises_not_found_for_an_unknown_model(sdk: AsyncOpenAI) -> None:
    with pytest.raises(NotFoundError, match="does not exist"):
        await sdk.chat.completions.create(model="gpt-9", messages=HELLO)


CALL: list[ChatCompletionMessageParam] = [
    {"role": "user", "content": '[[call: get_weather {"city": "Izmir"}]] Weather in Izmir?'}
]


async def test_sdk_reads_a_tool_call(sdk: AsyncOpenAI) -> None:
    completion = await sdk.chat.completions.create(model="fake-chat", messages=CALL)

    choice = completion.choices[0]
    assert choice.finish_reason == "tool_calls"
    assert choice.message.tool_calls is not None
    [call] = choice.message.tool_calls
    assert call.type == "function"
    assert (call.id, call.function.name) == ("call_fake_000001", "get_weather")
    assert json.loads(call.function.arguments) == {"city": "Izmir"}


async def test_sdk_puts_a_streamed_tool_call_together(sdk: AsyncOpenAI) -> None:
    stream = await sdk.chat.completions.create(model="fake-chat", messages=CALL, stream=True)
    name, arguments, finish_reason = "", "", None
    async for chunk in stream:
        choice = chunk.choices[0]
        for part in choice.delta.tool_calls or []:
            assert part.index == 0
            assert part.function is not None
            name += part.function.name or ""
            arguments += part.function.arguments or ""
        finish_reason = choice.finish_reason or finish_reason

    assert name == "get_weather"
    assert json.loads(arguments) == {"city": "Izmir"}
    assert finish_reason == "tool_calls"


async def test_sdk_raises_its_rate_limit_error_for_a_scripted_429(sdk: AsyncOpenAI) -> None:
    messages: list[ChatCompletionMessageParam] = [{"role": "user", "content": "[[fail: 429]] hi"}]
    with pytest.raises(RateLimitError, match="scripted failure"):
        await sdk.chat.completions.create(model="fake-chat", messages=messages)
