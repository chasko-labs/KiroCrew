"""A background call that ends after its slot moved to a new unit grows no older unit.

The entry is written to the slot's newest unit with ``for_session`` naming the
session that incurred it, so the savepoint beside the newest unit stays valid and a
restart reads nothing from the older unit, whatever its size.

Real store, real emitter, real slot read path; reads are counted per unit with a
wrapper around ``CrewLog.iter_from``.
"""

from __future__ import annotations

import itertools
from typing import Any

import pytest
from crew_log_drain import assert_drained

from kiro_crew import crew_log as lg
from kiro_crew.crew_log import CrewLog
from kiro_crew.crew_log import checkpoint as savepoints
from kiro_crew.crew_log import eager as crew_log_eager
from kiro_crew.crew_log import emit as crew_log_emit
from kiro_crew.crew_log import projection as crew_log
from kiro_crew.crew_log.projection_readers import _slot_units_for_fold
from kiro_crew.crew_log.store_paths import crew_log_dir

SLOT = "member-bg-reset"
FOLD = "workstreams"


@pytest.fixture(autouse=True)
def _home(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    _floor_monkeypatch.setenv("KIROCREW_CREW_LOG", "1")
    _floor_monkeypatch.setattr(crew_log_eager, "note_commit", lambda *a, **k: None)
    _floor_monkeypatch.setattr(savepoints, "MIN_ADVANCE_ENTRIES", 2)
    from kiro_crew.crew_log import store as crew_log_store

    ticks = itertools.count(1_700_000_000_000, 1_000)
    _floor_monkeypatch.setattr(
        crew_log_store, "now_ms", lambda: next(ticks)  # flake-ok: a fixed clock pin
    )
    crew_log_emit.reset_caches()
    crew_log_eager.stop_for_tests()
    crew_log.forget_slot_folds()
    yield
    crew_log.forget_slot_folds()
    crew_log_emit.reset_caches()


class _Provider:
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id


class _Sessions:
    def __init__(self, unit: str, slot: str = SLOT) -> None:
        self.unit = unit
        self.slot = slot

    def get_provider(self, key: str) -> Any:
        return _Provider(self.unit) if key == self.slot else None


class _Usage:
    credits = 1.5
    input_tokens = 40
    output_tokens = 4
    cache_read_tokens = 0
    cache_creation_tokens = 0


def _open(unit: str, slot: str = SLOT) -> None:
    crew_log_emit.on_session_opened(
        unit, agent="kirocrew", slot=slot, model="m", cwd="/w", owner="default"
    )


def _turns(unit: str, count: int) -> None:
    for n in range(count):
        crew_log_emit.on_turn_started(unit, n + 1, "user")


def _read() -> Any:
    units = _slot_units_for_fold(SLOT, FOLD)
    return crew_log.fold_slot_warm(FOLD, units, slot=SLOT).state


def _unit_files(unit: str) -> "dict[str, int]":
    directory = crew_log_dir(lg.KIND_SESSION, unit)
    return {p.name: p.stat().st_size for p in directory.iterdir() if p.is_file()}


def _restart_reads() -> "dict[str, int]":
    crew_log.forget_slot_folds()
    read: dict[str, int] = {}
    real = CrewLog.iter_from

    def counted(self, *args, **kwargs):
        for entry in real(self, *args, **kwargs):
            read[self.id] = read.get(self.id, 0) + 1
            yield entry

    CrewLog.iter_from = counted  # type: ignore[method-assign]
    try:
        _read()
    finally:
        CrewLog.iter_from = real  # type: ignore[method-assign]
    return read


@pytest.mark.parametrize("filler", [100, 400, 1_600])
def test_a_restart_after_a_background_call_across_a_reset_reads_nothing_from_the_old_unit(
    filler,
):
    """Red on main: the entry grows the old unit, the newest unit's savepoint does not
    describe the slot, and the restart re-reads the old unit. Here it reads zero."""
    from kiro_crew.llm_helpers import _background_crew_log_owner, _record_background_crew_log

    old, new = f"old-{filler}", f"new-{filler}"
    _open(old)
    _turns(old, filler)
    assert_drained()
    sessions = _Sessions(old)
    owner = _background_crew_log_owner(sessions, SLOT, "title")
    assert owner == old
    # The slot resets while the call runs: a new unit opens and the slot reads warm.
    _open(new)
    _turns(new, 1)
    assert_drained()
    sessions.unit = new
    _read()
    before = _unit_files(old)

    _record_background_crew_log(
        owner,
        "title",
        _Usage(),
        model="m",
        provider="acp",
        elapsed_ms=5,
        sessions=sessions,
        crew_log_session_key=SLOT,
    )
    assert_drained()

    assert _unit_files(old) == before, "the old unit's log and cuts did not grow"
    written = [
        e
        for e in CrewLog.open(lg.KIND_SESSION, new).iter_from(1)
        if e.type == "background/completed"
    ]
    assert [e.data.get("for_session") for e in written] == [old]

    read = _restart_reads()
    assert read.get(old, 0) == 0, read
    assert read.get(new, 0) <= 3, read


def _spend_after_one_call(slot: str, reset: bool) -> "tuple[float, dict[str, Any]]":
    """One billed background call on *slot*, with or without a reset during it.

    Answers the slot's usage total (summed over its units) and its per-session spend
    row, the split the ``workstreams`` fold keys by session.
    """
    from kiro_crew.llm_helpers import _background_crew_log_owner, _record_background_crew_log

    first, second = f"{slot}-a", f"{slot}-b"
    _open(first, slot)
    _turns(first, 3)
    assert_drained()
    sessions = _Sessions(first, slot)
    owner = _background_crew_log_owner(sessions, slot, "summary")
    if reset:
        _open(second, slot)
        _turns(second, 1)
        assert_drained()
        sessions.unit = second
    _record_background_crew_log(
        owner,
        "summary",
        _Usage(),
        model="m",
        provider="acp",
        elapsed_ms=5,
        sessions=sessions,
        crew_log_session_key=slot,
    )
    assert_drained()
    units = _slot_units_for_fold(slot, FOLD)
    total = sum(crew_log.read_projection(unit, "usage").value["credits"] for unit in units)
    spend = crew_log.fold_slot_warm(FOLD, units, slot=slot).state["spend"]
    assert list(spend) == [slot], spend
    return total, {key: spend[slot][key] for key in ("credits", "reported", "duration_ms")}


def test_a_reset_during_the_call_charges_the_same_spend_exactly_once():
    """The same call gives the same slot total and the same per-session row, reset or not."""
    plain = _spend_after_one_call("member-bg-plain", reset=False)
    moved = _spend_after_one_call("member-bg-moved", reset=True)
    assert plain == moved
    assert plain[0] == pytest.approx(_Usage.credits)
    assert plain[1]["credits"] == pytest.approx(_Usage.credits)


def test_a_call_ending_before_the_new_unit_has_a_crew_log_is_held_until_it_opens():
    """Right after a reset the newest unit can have no crew log yet: the entry waits.

    It is written once the unit's first open creates the crew log, with the normal
    header, and the older unit is never written. Spend is counted once.
    """
    from kiro_crew.llm_helpers import _background_crew_log_owner, _record_background_crew_log

    old, new = "held-old", "held-new"
    _open(old)
    _turns(old, 3)
    assert_drained()
    sessions = _Sessions(old)
    owner = _background_crew_log_owner(sessions, SLOT, "title")
    sessions.unit = new  # reset: the slot now names a unit with no crew log
    before = _unit_files(old)
    _record_background_crew_log(
        owner,
        "title",
        _Usage(),
        model="m",
        provider="acp",
        elapsed_ms=5,
        sessions=sessions,
        crew_log_session_key=SLOT,
    )
    assert_drained()
    assert not CrewLog.exists(lg.KIND_SESSION, new), "no crew log is created from here"
    assert _unit_files(old) == before
    _open(new)
    assert_drained()
    entries = list(CrewLog.open(lg.KIND_SESSION, new).iter_from(1))
    types = [e.type for e in entries]
    assert types.index("session/opened") < types.index("background/completed")
    (held,) = [e for e in entries if e.type == "background/completed"]
    assert held.data["for_session"] == old
    assert _unit_files(old) == before, "the older unit never grows"
    units = _slot_units_for_fold(SLOT, FOLD)
    total = sum(crew_log.read_projection(unit, "usage").value["credits"] for unit in units)
    assert total == pytest.approx(_Usage.credits)


def test_held_writes_past_the_bound_are_counted_and_warned_once(caplog, _floor_monkeypatch):
    """Past the per-unit bound a held write is discarded, counted, and warned once."""
    _floor_monkeypatch.setattr(crew_log_emit, "_AWAITING_OPEN_PER_UNIT", 2)
    _floor_monkeypatch.setattr(
        crew_log_emit, "_AWAITING_OPEN", type(crew_log_emit._AWAITING_OPEN)()
    )
    _floor_monkeypatch.setattr(crew_log_emit, "_awaiting_open_warned", set())
    _floor_monkeypatch.setattr(crew_log_emit, "_awaiting_open_counts", {"dropped": 0})
    ran: list[int] = []
    for n in range(4):
        crew_log_emit.write_when_opened("never-opened", lambda n=n: ran.append(n))
    assert ran == []
    assert crew_log_emit.awaiting_open_dropped() == 2
    warnings = [r for r in caplog.records if "never-opened" in r.getMessage()]
    assert len(warnings) == 1


def test_held_units_past_the_bound_drop_the_oldest(_floor_monkeypatch):
    _floor_monkeypatch.setattr(crew_log_emit, "_AWAITING_OPEN_UNITS", 2)
    _floor_monkeypatch.setattr(
        crew_log_emit, "_AWAITING_OPEN", type(crew_log_emit._AWAITING_OPEN)()
    )
    _floor_monkeypatch.setattr(crew_log_emit, "_awaiting_open_warned", set())
    _floor_monkeypatch.setattr(crew_log_emit, "_awaiting_open_counts", {"dropped": 0})
    for sid in ("u1", "u2", "u3"):
        crew_log_emit.write_when_opened(sid, lambda: None)
    assert list(crew_log_emit._AWAITING_OPEN) == ["u2", "u3"]
    assert crew_log_emit.awaiting_open_dropped() == 1
