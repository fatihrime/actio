"""The official OpenAI SDK reads fakellm's responses: proof that the wire format is right."""

from collections.abc import AsyncIterator

import httpx2
import pytest
from fastapi import FastAPI
from openai import AsyncOpenAI, NotFoundError
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
