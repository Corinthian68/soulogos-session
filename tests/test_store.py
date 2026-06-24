import pytest
from pathlib import Path
from soulogos_session.store import SessionStore


@pytest.fixture
async def store(tmp_path: Path) -> SessionStore:
    s = SessionStore(tmp_path / "test.sqlite")
    await s.init()
    return s


async def test_create_session(store: SessionStore) -> None:
    sid = await store.create_session(guild_id=111, channel_id=222)
    assert isinstance(sid, int)
    assert sid > 0


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
