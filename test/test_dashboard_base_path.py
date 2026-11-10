"""Serving the dashboard under a URL sub-path (``KIROCREW_BASE_PATH``).

The frontend build already supports a base (Vite ``base`` + transport/DOM
shims installed first in ``main.tsx``); the gateway mounts the whole
dashboard app under the same prefix via ``add_subapp``. These tests pin the
prefix normalization and the mount mechanism: routes answer under the base
and nothing answers at the root.
"""

from __future__ import annotations

import pytest

from kiro_crew.dashboard.urls import resolve_base_path as resolve_dashboard_base_path


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        (None, ""),
        ("", ""),
        ("/", ""),
        ("/crew", "/crew"),
        ("/crew/", "/crew"),
        ("  /crew  ", "/crew"),
        ("/a/b", "/a/b"),
        ("/a-b_c.d~e", "/a-b_c.d~e"),
    ],
)
def test_resolve_base_path_accepts_plain_paths(raw: str | None, want: str) -> None:
    assert resolve_dashboard_base_path(raw) == want


@pytest.mark.parametrize(
    "raw",
    ["a/b", "//x", "http://x/crew", "/crew?x", "/crew#x", "/../", "/a/../b", "/a b", "/a?b"],
)
def test_resolve_base_path_refuses_non_paths(raw: str) -> None:
    with pytest.raises(ValueError):
        resolve_dashboard_base_path(raw)


@pytest.mark.asyncio
async def test_subapp_mount_answers_under_prefix_only() -> None:
    """The mechanism ``start_dashboard`` relies on: prefix stripped for the
    sub-app, root stays unanswered."""
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    async def ping(_request: web.Request) -> web.Response:
        return web.Response(text="pong")

    sub = web.Application()
    sub.router.add_get("/api/ping", ping)
    root = web.Application()
    root.add_subapp("/crew", sub)
    client = TestClient(TestServer(root))
    await client.start_server()
    try:
        async with client.get("/crew/api/ping") as resp:
            assert resp.status == 200
            assert await resp.text() == "pong"
        async with client.get("/api/ping") as resp:
            assert resp.status == 404
    finally:
        await client.close()
