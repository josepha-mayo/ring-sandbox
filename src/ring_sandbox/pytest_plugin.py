"""pytest fixtures: an in-process emulator and a client wired to it.

No sockets are opened; the client talks to the FastAPI app through an ASGI transport.
"""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest

from .client import RingClient
from .emulator import create_app
from .world import World, default_world


@pytest.fixture
def ring_world() -> World:
    return default_world()


@pytest.fixture
def ring_app(ring_world: World):
    return create_app(ring_world)


@pytest.fixture
def ring_transport(ring_app) -> httpx.BaseTransport:
    return _SyncASGITransport(ring_app)


@pytest.fixture
def ring_client(ring_transport: httpx.BaseTransport) -> Iterator[RingClient]:
    with RingClient("sandbox-token", base_url="http://sandbox", transport=ring_transport) as c:
        yield c


@pytest.fixture
def ring_control(ring_transport: httpx.BaseTransport) -> Iterator[httpx.Client]:
    """Plain client for ``/_sandbox/*`` control routes."""
    with httpx.Client(base_url="http://sandbox", transport=ring_transport) as c:
        yield c


class _SyncASGITransport(httpx.BaseTransport):
    """Drive an ASGI app from synchronous httpx by running each request on a fresh loop."""

    def __init__(self, app) -> None:
        self._app = app

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        import asyncio

        async def go() -> httpx.Response:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self._app), base_url="http://sandbox"
            ) as ac:
                resp = await ac.request(
                    request.method, request.url, headers=request.headers, content=request.content
                )
                await resp.aread()
                return httpx.Response(
                    resp.status_code, headers=resp.headers, content=resp.content, request=request
                )

        return asyncio.run(go())
