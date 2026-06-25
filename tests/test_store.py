import re
import pytest
from pathlib import Path
from soulogos_session.store import SessionStore

_SESSION_ID_RE = re.compile(r"^\d{8}_\d{6}(_\d+)?$")


@pytest.fixture
async def store(tmp_path: Path) -> SessionStore:
    s = SessionStore(tmp_path / "test.sqlite")
    await s.init()
    return s


async def test_create_session(store: SessionStore) -> None:
    sid = await store.create_session(guild_id=111, channel_id=222)
    assert isinstance(sid, str)
    assert _SESSION_ID_RE.match(sid), f"Unexpected session ID format: {sid!r}"


async def test_add_and_get_lines(store: SessionStore) -> None:
    sid = await store.create_session(guild_id=111, channel_id=222)
    await store.add_line(
        session_id=sid,
        discord_user_id=999,
        display_name="Thalindra",
        text="I cast fireball.",
        confidence=0.92,
    )
    lines = await store.get_lines(sid)
    assert len(lines) == 1
    line = lines[0]
    assert line["text"] == "I cast fireball."
    assert line["display_name"] == "Thalindra"
    assert line["discord_user_id"] == 999
    assert abs(line["confidence"] - 0.92) < 1e-6


async def test_end_session(store: SessionStore) -> None:
    sid = await store.create_session(guild_id=111, channel_id=222)
    await store.end_session(sid)
    await store.init()  # re-init is idempotent


async def test_get_lines_empty(store: SessionStore) -> None:
    sid = await store.create_session(guild_id=111, channel_id=222)
    assert await store.get_lines(sid) == []


async def test_multiple_sessions_isolated(store: SessionStore) -> None:
    sid1 = await store.create_session(guild_id=1, channel_id=1)
    sid2 = await store.create_session(guild_id=2, channel_id=2)
    await store.add_line(session_id=sid1, discord_user_id=1, display_name="A", text="hello", confidence=0.8)
    await store.add_line(session_id=sid2, discord_user_id=2, display_name="B", text="world", confidence=0.9)
    assert len(await store.get_lines(sid1)) == 1
    assert len(await store.get_lines(sid2)) == 1


async def test_list_sessions_empty(store: SessionStore) -> None:
    sessions = await store.list_sessions(guild_id=111)
    assert sessions == []


async def test_list_sessions(store: SessionStore) -> None:
    sid = await store.create_session(guild_id=111, channel_id=222)
    await store.add_line(session_id=sid, discord_user_id=1, display_name="A", text="x", confidence=1.0)
    await store.add_line(session_id=sid, discord_user_id=1, display_name="A", text="y", confidence=1.0)

    sessions = await store.list_sessions(guild_id=111)
    assert len(sessions) == 1
    assert sessions[0]["id"] == sid
    assert sessions[0]["line_count"] == 2
    assert sessions[0]["guild_id"] == 111


async def test_list_sessions_guild_scoped(store: SessionStore) -> None:
    await store.create_session(guild_id=111, channel_id=1)
    await store.create_session(guild_id=222, channel_id=2)

    assert len(await store.list_sessions(guild_id=111)) == 1
    assert len(await store.list_sessions(guild_id=222)) == 1
    assert len(await store.list_sessions(guild_id=999)) == 0


async def test_delete_session(store: SessionStore) -> None:
    sid = await store.create_session(guild_id=111, channel_id=222)
    await store.add_line(session_id=sid, discord_user_id=1, display_name="A", text="hello", confidence=0.9)

    deleted = await store.delete_session(sid)
    assert deleted is True
    assert await store.get_lines(sid) == []
    assert await store.list_sessions(guild_id=111) == []


async def test_delete_session_not_found(store: SessionStore) -> None:
    deleted = await store.delete_session("19990101_000000")
    assert deleted is False


async def test_delete_session_guild_scoped(store: SessionStore) -> None:
    sid = await store.create_session(guild_id=111, channel_id=1)

    # Wrong guild -- should not delete
    deleted = await store.delete_session(sid, guild_id=999)
    assert deleted is False
    assert len(await store.list_sessions(guild_id=111)) == 1

    # Correct guild -- should delete
    deleted = await store.delete_session(sid, guild_id=111)
    assert deleted is True
    assert await store.list_sessions(guild_id=111) == []


async def test_session_id_collision_handling(store: SessionStore) -> None:
    # Force two sessions with the same timestamp by monkeypatching
    import soulogos_session.store as store_module
    original_now_fn = store_module.datetime

    call_count = 0

    class _FakeDatetime:
        @staticmethod
        def now(tz=None):
            nonlocal call_count
            call_count += 1
            from datetime import datetime, timezone
            return datetime(2026, 6, 24, 13, 10, 25, tzinfo=timezone.utc)

    store_module.datetime = _FakeDatetime  # type: ignore[attr-defined]
    try:
        sid1 = await store.create_session(guild_id=1, channel_id=1)
        sid2 = await store.create_session(guild_id=1, channel_id=1)
    finally:
        store_module.datetime = original_now_fn

    assert sid1 == "20260624_131025"
    assert sid2 == "20260624_131025_2"
