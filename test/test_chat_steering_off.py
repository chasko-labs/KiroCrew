"""A chat can switch one steering file off where Crew writes that file's text.

The store (``kiro_crew.steering_off``), the two writers that honour it (the Claude
Code ``[Steering resources]`` load and the folder steering section), the turn text
with and without an exclusion, and the switch route.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew import pinned_fs
from kiro_crew import steering_off as store
from kiro_crew.agent_sdk.provider_identity import PROVIDER_CLAUDE_CODE
from kiro_crew.context import ContextBuilder
from kiro_crew.context_assembly import inclusion
from kiro_crew.dashboard import chat_handlers
from kiro_crew.dashboard.handlers import chat_steering
from kiro_crew.dashboard.state import DashboardState, _ChatSlot
from kiro_crew.learn import LessonStore
from kiro_crew.memory import MemoryStore
from kiro_crew.skills import SkillsLoader

RULE = "GLOBAL-STEERING-RULE-3b7e"
FOLDER_RULE = "FOLDER-STEERING-RULE-9d1c"


@pytest.fixture(autouse=True)
def _close_skills_loaders(close_skills_loaders):
    """Every test here may build a ``ContextBuilder`` (``test/conftest.py``)."""


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A home with one global ``always`` guide, the shipped agent, and a data home."""
    steering = tmp_path / ".kiro" / "steering"
    steering.mkdir(parents=True)
    (steering / "global.md").write_text(f"# Global\n{RULE}\n", encoding="utf-8")
    agents = tmp_path / ".kiro" / "agents"
    agents.mkdir(parents=True)
    (agents / "kirocrew.json").write_text(
        json.dumps({"resources": ["file://.kiro/steering/**/*.md"]}), encoding="utf-8"
    )
    monkeypatch.setattr(store, "_store_path", lambda: tmp_path / "data" / store._FILE_NAME)
    with patch("pathlib.Path.home", return_value=tmp_path):
        yield tmp_path


@pytest.fixture
def standards(tmp_path):
    root = tmp_path / "standards"
    root.mkdir()
    (root / "coding.md").write_text(f"# Coding\n{FOLDER_RULE}\n", encoding="utf-8")
    return root


def _builder(tmp_path) -> ContextBuilder:
    return ContextBuilder(
        memory=MemoryStore(workspace=tmp_path / "ws"),
        skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
        lessons=LessonStore(base_dir=tmp_path),
    )


def _resolved(path: Path) -> str:
    return str(path.resolve())


def test_an_agent_cannot_write_the_store():
    """The record sits on both fences that keep it agent-unwritable.

    The file tool refuses the path, and the sandbox mounts the leaf read-only
    (pre-created, so an absent file cannot be created from inside either). Only the
    gateway's dashboard route, which runs outside the sandbox, writes it.
    """
    from kiro_crew import sandbox, security
    from kiro_crew.config.paths import data_home

    leaf = store._FILE_NAME
    assert store._store_path() == data_home() / leaf
    assert leaf in security._CREW_SECRET_LEAVES
    assert security.is_sensitive_path(f"~/.kiro/crew/{leaf}") is True
    assert security.is_sensitive_write_path(f"~/.kiro/crew/{leaf}") is True
    assert leaf in sandbox._CREW_READONLY_LEAVES
    assert leaf in sandbox._CREW_PRECREATE_READONLY_FILE_LEAVES


def test_the_store_keeps_paths_per_chat(home):
    assert store.steering_off_for("main") == frozenset()
    assert store.set_steering_off("main", "/a.md", True) == {"/a.md"}
    store.set_steering_off("other", "/b.md", True)
    assert store.steering_off_for("main") == {"/a.md"}
    assert store.set_steering_off("main", "/a.md", False) == frozenset()
    assert store.steering_off_for("main") == frozenset()
    assert store.steering_off_for("other") == {"/b.md"}


def test_a_write_keeps_the_record_on_its_inode(home):
    """A rename would detach a live sandbox's read-only bind; the rewrite does not."""
    store.set_steering_off("main", "/a.md", True)
    inode = store._store_path().stat().st_ino
    store.set_steering_off("main", "/b.md", True)
    assert store._store_path().stat().st_ino == inode


def test_a_planted_link_at_the_record_is_refused(home, tmp_path):
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text("{}", encoding="utf-8")
    path = store._store_path()
    path.parent.mkdir(parents=True)
    try:
        path.symlink_to(elsewhere)
    except OSError:
        pytest.skip("this host cannot create a symlink")
    with pytest.raises(OSError):
        store.set_steering_off("main", "/a.md", True)
    assert elsewhere.read_text(encoding="utf-8") == "{}"


def test_a_switch_never_overwrites_an_unreadable_store(home):
    """Every other chat's record survives a broken file; the write refuses instead."""
    path = store._store_path()
    path.parent.mkdir(parents=True)
    path.write_text('{"other": ["/b.md"]', encoding="utf-8")
    with pytest.raises(store.SteeringOffStoreUnreadable):
        store.set_steering_off("main", "/a.md", True)
    assert path.read_text(encoding="utf-8") == '{"other": ["/b.md"]'


def test_an_empty_pre_created_store_is_a_valid_empty_one(home):
    path = store._store_path()
    path.parent.mkdir(parents=True)
    path.write_text("", encoding="utf-8")
    assert store.set_steering_off("main", "/a.md", True) == {"/a.md"}


def test_every_caller_of_the_builder_honours_the_store(home):
    """A channel or messaging turn passes no set; the builder reads the chat's own."""
    store.set_steering_off("slack:1700", _resolved(home / ".kiro" / "steering" / "global.md"), True)
    msg, _ = _builder(home).build_message(
        "hello", True, "slack:1700", provider_type=PROVIDER_CLAUDE_CODE
    )
    assert RULE not in msg
    msg, _ = _builder(home).build_message(
        "hello", True, "slack:1701", provider_type=PROVIDER_CLAUDE_CODE
    )
    assert RULE in msg


def test_an_unreadable_store_reads_as_nothing_switched_off(home):
    path = store._store_path()
    path.parent.mkdir(parents=True)
    path.write_text("not json", encoding="utf-8")
    assert store.steering_off_for("main") == frozenset()


def test_the_claude_code_load_leaves_out_a_switched_off_file(home):
    off = frozenset({_resolved(home / ".kiro" / "steering" / "global.md")})
    msg, _ = _builder(home).build_message(
        "hello", True, "main", provider_type=PROVIDER_CLAUDE_CODE, steering_off=off
    )
    assert RULE not in msg
    msg, _ = _builder(home).build_message("hello", True, "main", provider_type=PROVIDER_CLAUDE_CODE)
    assert RULE in msg


def test_compaction_reinjection_leaves_it_out_too(home):
    off = frozenset({_resolved(home / ".kiro" / "steering" / "global.md")})
    msg, _ = _builder(home).build_message(
        "carry on",
        is_new_session=False,
        needs_reinjection=True,
        provider_type=PROVIDER_CLAUDE_CODE,
        steering_off=off,
    )
    assert RULE not in msg


@pytest.mark.skipif(
    not pinned_fs.supports_pinned_tree_walk(),
    reason="folder steering refuses to walk by name on this platform",
)
def test_the_folder_section_leaves_out_a_switched_off_file(home, standards):
    off = frozenset({_resolved(standards / "coding.md")})
    msg, _ = _builder(home).build_message(
        "hello",
        True,
        "main",
        provider_type="codex",
        steering_dirs=(str(standards),),
        steering_off=off,
    )
    assert FOLDER_RULE not in msg
    msg, _ = _builder(home).build_message(
        "hello", True, "main", provider_type="codex", steering_dirs=(str(standards),)
    )
    assert FOLDER_RULE in msg


@pytest.mark.skipif(
    not pinned_fs.supports_pinned_tree_walk(),
    reason="folder steering refuses to walk by name on this platform",
)
def test_with_nothing_switched_off_both_writers_give_the_old_text(home, standards):
    """Golden: an empty set takes exactly the call main makes."""
    from kiro_crew import context as ctx

    cap = ctx._resolve_caps(None).steering
    assert inclusion._render_folder_steering_section(
        (str(standards),), None, cap, steering_off=frozenset()
    ) == ctx.render_folder_steering(
        ctx.collect_folder_steering((str(standards),), project=None),
        max_chars=cap,
        scrub=ctx._neutralize_structural_markers,
    )
    kwargs = dict(agent=None, is_cc=True, essentials="", context_groups=None, project=None)
    assert inclusion.steering_resources_part(
        **kwargs, steering_off=frozenset()
    ) == ctx._load_steering_resources(None)


def _state(slot: _ChatSlot) -> DashboardState:
    state = MagicMock(spec=DashboardState)
    state._slots = {slot.key: slot}
    state.sessions = MagicMock()
    state.sessions.get_provider = MagicMock(return_value=None)
    return state


async def _call(state: DashboardState, method: str, body: object = None) -> tuple[int, dict]:
    app = web.Application()
    app["state"] = state
    app.router.add_get("/api/chat/slots/{slot}/steering", chat_steering.api_chat_slot_steering)
    app.router.add_put(
        "/api/chat/slots/{slot}/steering", chat_steering.api_chat_slot_steering_switch
    )
    async with TestClient(TestServer(app)) as client:
        resp = await client.request(method, "/api/chat/slots/main/steering", json=body)
        return resp.status, await resp.json()


@pytest.fixture
def claude_chat(home, monkeypatch):
    """A cold chat on the Claude Code harness whose turns run the default agent."""
    import kiro_crew.session_agent_selection as selection

    monkeypatch.setattr(
        chat_handlers.KiroCrewConfig,
        "load",
        lambda: SimpleNamespace(
            agent=SimpleNamespace(acp_backend="claude", member_acp_backend=""), agents={}
        ),
    )
    monkeypatch.setattr(
        selection,
        "resolve_session_agent_bindings",
        lambda *_a, **_k: SimpleNamespace(kiro_agent="kirocrew"),
    )
    return _state(_ChatSlot("main"))


@pytest.mark.asyncio
async def test_switching_a_file_off_is_stored_and_listed(claude_chat, home):
    status, data = await _call(claude_chat, "GET")
    row = next(r for r in data["files"] if r["name"] == "global.md")
    assert (status, row["reaches"], row["switch"]) == (200, "yes", "on")
    assert "resolved" not in row
    status, data = await _call(claude_chat, "PUT", {"path": row["path"], "off": True})
    row = next(r for r in data["files"] if r["name"] == "global.md")
    assert (status, row["reaches"], row["reason"], row["switch"]) == (
        200,
        "no",
        "switched_off",
        "off",
    )
    assert store.steering_off_for("dashboard:main") == {
        _resolved(home / ".kiro" / "steering" / "global.md")
    }
    status, data = await _call(claude_chat, "PUT", {"path": row["path"], "off": False})
    assert store.steering_off_for("dashboard:main") == frozenset()


@pytest.mark.asyncio
async def test_a_file_crew_does_not_write_cannot_be_switched(home, monkeypatch):
    import kiro_crew.session_agent_selection as selection

    monkeypatch.setattr(
        chat_handlers.KiroCrewConfig,
        "load",
        lambda: SimpleNamespace(
            agent=SimpleNamespace(acp_backend="codex", member_acp_backend=""), agents={}
        ),
    )
    monkeypatch.setattr(
        selection,
        "resolve_session_agent_bindings",
        lambda *_a, **_k: SimpleNamespace(kiro_agent="kirocrew"),
    )
    state = _state(_ChatSlot("main"))
    _status, data = await _call(state, "GET")
    path = next(r for r in data["files"] if r["name"] == "global.md")["path"]
    status, data = await _call(state, "PUT", {"path": path, "off": True})
    assert (status, data["code"]) == (409, "not_switchable")
    assert store.steering_off_for("dashboard:main") == frozenset()


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [{"path": "", "off": True}, {"path": "x.md", "off": "yes"}, []])
async def test_a_malformed_switch_is_refused(claude_chat, body):
    status, _data = await _call(claude_chat, "PUT", body)
    assert status == 400


@pytest.mark.asyncio
async def test_an_unreadable_store_refuses_the_switch(claude_chat):
    path = store._store_path()
    path.parent.mkdir(parents=True)
    path.write_text("not json", encoding="utf-8")
    _status, data = await _call(claude_chat, "GET")
    row = next(r for r in data["files"] if r["name"] == "global.md")
    status, data = await _call(claude_chat, "PUT", {"path": row["path"], "off": True})
    assert (status, data["code"]) == (409, "store_unreadable")
    assert path.read_text(encoding="utf-8") == "not json"


@pytest.mark.asyncio
async def test_the_switch_takes_the_session_aware_app_gate(claude_chat, monkeypatch):
    """An app must own the session as well as the slot, as other per-slot writes do."""
    seen: list[str] = []

    def gate(_app, _slot, _name, operation):
        seen.append(operation)
        return web.json_response({"error": "nope"}, status=404)

    from kiro_crew.dashboard import slot_ownership

    monkeypatch.setattr(slot_ownership, "deny_app_slot_session_access", gate)
    status, _data = await _call(claude_chat, "PUT", {"path": "x.md", "off": True})
    assert (status, seen) == (404, ["slot_steering_switch"])
