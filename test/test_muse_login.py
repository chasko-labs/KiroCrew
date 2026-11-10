"""``/api/muse-login/*``: the in-product Muse subscription login, over HTTP.

The owner starts a ``muse login`` device flow from the backends panel and
approves the code in their own browser; the gateway host never sees one.
These tests drive the real route table with a fake ``muse`` binary, so the
router, the owner gate, the spawn, the output parsing and the single-flight
rule all take part. The only things stubbed are the binary resolution (PATH
to a script) and the credential-file path (a scratch file the script
touches), which are the two subjects, not the scaffolding.

What this does NOT cover: a human approving a real code against Meta's
device endpoint, because that needs the owner's browser. The transcript the
fake prints is the real one captured from ``muse login`` (1.4.15), so the
parser is pinned against the genuine text.
"""

from __future__ import annotations

import stat

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import kiro_crew.acp.harness.muse as muse_harness
from kiro_crew.dashboard.handlers import muse_login
from kiro_crew.dashboard.routes import agent_config as agent_config_routes

pytestmark = pytest.mark.asyncio

_OWNER = "owner-muse-login"

#: Captured from ``muse login`` (1.4.15) under a pty. The parser pins this.
_TRANSCRIPT = (
    "Open this page to sign in:\r\n"
    "  https://auth.meta.com/oauth/device/?code=CLFM-PPVW\r\n"
    "confirm this code matches:\r\n"
    "  CLFM-PPVW\r\n"
    "\r\n"
    "Press Enter to open it in your browser:\r\n"
)


class _State:
    owner_id = _OWNER


@pytest.fixture
def owner_middleware():
    @web.middleware
    async def _mw(request, handler):
        request["app"] = ""
        request["user"] = _OWNER
        return await handler(request)

    return _mw


@pytest.fixture
def stranger_middleware():
    @web.middleware
    async def _mw(request, handler):
        request["app"] = ""
        request["user"] = "stranger"
        return await handler(request)

    return _mw


@pytest_asyncio.fixture(autouse=True)
async def _isolated_session():
    """No login leaks between tests; a pending child is always reaped."""
    with muse_login._lock:
        muse_login._session = None
    yield
    with muse_login._lock:
        session = muse_login._session
        muse_login._session = None
    if session is not None and session.state == "pending":
        try:
            session.proc.kill()
        except ProcessLookupError:
            pass
        try:
            await session.proc.wait()
        except Exception:
            pass


async def _client(middleware) -> TestClient:
    app = web.Application(middlewares=[middleware])
    app["state"] = _State()
    agent_config_routes.register(app)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


def _fake_muse(directory, body: str):
    """An executable named ``muse`` printing *body*, then sleeping.

    The sleep keeps it pending so status/rejoin/cancel have something to
    observe; the teardown fixture reaps it.
    """
    target = directory / "muse"
    # ``%b``: the transcript's ``\r\n`` escapes must reach the parser as real
    # carriage returns, or the test pins a decoding nobody performs.
    target.write_text(f"#!/bin/sh\nprintf '%b' '{body}'\nsleep 30\n", encoding="utf-8")
    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return target


class TestConnectFlowFlag:
    def test_only_the_muse_row_carries_the_flow(self):
        from types import SimpleNamespace

        from kiro_crew.acp_backends import ACP_BACKEND_KIRO, ACP_BACKEND_MUSE
        from kiro_crew.agent_sdk import MISSING
        from kiro_crew.dashboard.handlers.acp_backend_status import _row

        def row_for(backend):
            return _row(
                SimpleNamespace(
                    backend=backend,
                    policy_id="x",
                    installed=MISSING,
                    missing_components=(),
                    install_command="",
                    found_version="",
                    restart_required=False,
                ),
                set(),
            )

        assert row_for(ACP_BACKEND_MUSE)["connect_flow"] == "muse-login"
        assert row_for(ACP_BACKEND_KIRO)["connect_flow"] is None


class TestParseLoginOutput:
    def test_real_transcript_yields_code_and_url(self):
        code, url = muse_login.parse_login_output(_TRANSCRIPT)
        assert code == "CLFM-PPVW"
        assert url == "https://auth.meta.com/oauth/device/?code=CLFM-PPVW"

    def test_mismatched_code_is_refused(self):
        text = _TRANSCRIPT.replace("CLFM-PPVW\r\n\r\n", "AAAA-BBBB\r\n\r\n")
        assert muse_login.parse_login_output(text) == ("", "")

    def test_partial_output_is_not_a_grant(self):
        assert muse_login.parse_login_output("Open this page to sign in:\r\n") == ("", "")


class TestLoginFlowOverHttp:
    async def test_start_status_cancel_roundtrip(self, tmp_path, monkeypatch, owner_middleware):
        bindir = tmp_path / "bin"
        bindir.mkdir()
        fake = _fake_muse(bindir, _TRANSCRIPT)
        # Pinned, not PATH-resolved: a host with a real ``muse`` must not
        # change what the handler spawns (or the cancel test below would
        # drive a genuine device flow).
        monkeypatch.setattr(
            muse_harness, "_resolve_muse_bin", lambda: (str(fake), "test")
        )
        client = await _client(owner_middleware)
        try:
            resp = await client.post("/api/muse-login/start")
            assert resp.status == 200
            started = await resp.json()
            assert started["ok"] is True
            assert started["state"] == "pending"
            assert started["code"] == "CLFM-PPVW"
            assert started["url"].endswith("code=CLFM-PPVW")

            # Single-flight: a second start rejoins the pending one.
            resp = await client.post("/api/muse-login/start")
            rejoined = await resp.json()
            assert rejoined["rejoined"] is True
            assert rejoined["code"] == "CLFM-PPVW"

            resp = await client.get("/api/muse-login/status")
            assert (await resp.json())["state"] == "pending"

            resp = await client.post("/api/muse-login/cancel")
            assert resp.status == 200
            assert (await resp.json())["state"] == "cancelled"

            resp = await client.post("/api/muse-login/cancel")
            assert resp.status == 409
            assert (await resp.json())["code"] == "muse_login_not_pending"
        finally:
            await client.close()

    async def test_approved_login_reads_ready(self, tmp_path, monkeypatch, owner_middleware):
        """Exit 0 plus the credential file: the panel's success state."""
        bindir = tmp_path / "bin"
        bindir.mkdir()
        target = bindir / "muse"
        target.write_text(
            "#!/bin/sh\nprintf '%b' '" + _TRANSCRIPT + "'\n"
            "touch \"$MUSE_AUTH_FILE\"\n",
            encoding="utf-8",
        )
        target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        auth_file = tmp_path / "auth.json"
        monkeypatch.setenv("MUSE_AUTH_FILE", str(auth_file))
        monkeypatch.setattr(muse_login, "muse_auth_path", lambda: auth_file)
        monkeypatch.setattr(
            muse_harness, "_resolve_muse_bin", lambda: (str(target), "test")
        )
        client = await _client(owner_middleware)
        try:
            resp = await client.post("/api/muse-login/start")
            assert resp.status == 200
            for _ in range(40):
                status = await (await client.get("/api/muse-login/status")).json()
                if status["state"] != "pending":
                    break
                import asyncio as _aio

                await _aio.sleep(0.25)
            assert status["state"] == "ready", status
            # Terminal states stop carrying the grant.
            assert status["code"] == ""
            assert auth_file.is_file()
        finally:
            await client.close()

    async def test_no_muse_binary_is_a_coded_409(self, tmp_path, monkeypatch, owner_middleware):
        bindir = tmp_path / "bin"
        bindir.mkdir()
        monkeypatch.setenv("PATH", str(bindir))
        monkeypatch.setattr(muse_harness, "_resolve_muse_bin", lambda: (None, "test"))
        client = await _client(owner_middleware)
        try:
            resp = await client.post("/api/muse-login/start")
            assert resp.status == 409
            assert (await resp.json())["code"] == "muse_not_installed"
        finally:
            await client.close()

    async def test_stranger_is_refused_on_all_three(
        self, tmp_path, monkeypatch, stranger_middleware
    ):
        bindir = tmp_path / "bin"
        bindir.mkdir()
        monkeypatch.setenv("PATH", str(bindir))
        client = await _client(stranger_middleware)
        try:
            for method, path in (
                ("post", "/api/muse-login/start"),
                ("get", "/api/muse-login/status"),
                ("post", "/api/muse-login/cancel"),
            ):
                resp = await getattr(client, method)(path)
                assert resp.status in (401, 403), (path, resp.status)
        finally:
            await client.close()
