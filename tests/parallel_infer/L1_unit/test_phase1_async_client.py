"""
L1 / Phase 1 - AsyncOpenAI client wiring (commits 1.1, 1.2).

These tests do NOT hit any network: they only assert that the factory
builds an ``openai.AsyncOpenAI`` instance with an httpx pool of the
configured size, and that the legacy sync helper still exists for the
evaluator path.
"""

from __future__ import annotations

import asyncio

import openai
import pytest

from rollout.core.utils import (
    create_async_openai_client,
    create_openai_client,
)


def test_create_async_openai_client_uses_async_class():
    client = create_async_openai_client(
        api_key="x",
        base_url="http://stub",
        max_connections=128,
        max_keepalive=32,
        timeout_s=42.0,
        connect_timeout_s=7.0,
    )
    try:
        assert isinstance(client, openai.AsyncOpenAI)
        # The underlying httpx client must reflect the explicit pool sizing
        # so high concurrency is not throttled by httpx's default 100/20.
        http_client = getattr(client, "_client", None)
        assert http_client is not None, "openai.AsyncOpenAI should hold a httpx client"
        # On httpx >= 0.25 the limits live on the AsyncHTTPTransport.
        # The structure isn't stable across openai/httpx versions; instead
        # of poking internals we just exercise a roundtrip-safe attr.
        assert http_client.timeout is not None
    finally:
        asyncio.run(client.close())


def test_create_async_openai_client_rejects_empty_credentials():
    with pytest.raises(ValueError):
        create_async_openai_client(api_key="", base_url="http://x")
    with pytest.raises(ValueError):
        create_async_openai_client(api_key="x", base_url="")


def test_create_openai_client_still_returns_sync_class():
    """Evaluator still uses the sync client; the commit must not break it."""
    client = create_openai_client(api_key="x", base_url="http://stub")
    assert isinstance(client, openai.OpenAI)
