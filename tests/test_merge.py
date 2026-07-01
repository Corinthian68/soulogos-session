import pytest
from pathlib import Path
from soulogos_session.store import (
    _sync_init,
    _sync_create_session,
    _sync_add_line,
    _sync_get_lines,
    _sync_get_session,
    _sync_merge_sessions,
)

GUILD_ID = 111
OTHER_GUILD_ID = 999


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "test.sqlite"
    _sync_init(path)
    return path


def test_merge_moves_all_lines_to_target(db_path):
    target_id = _sync_create_session(db_path, guild_id=GUILD_ID, channel_id=1)
    source_id = _sync_create_session(db_path, guild_id=GUILD_ID, channel_id=2)
    _sync_add_line(db_path, source_id, 1, "Alice", "first line", 0.9)
    _sync_add_line(db_path, source_id, 1, "Alice", "second line", 0.85)

    result = _sync_merge_sessions(db_path, target_id, [source_id], guild_id=GUILD_ID)

    assert result["merged_count"] == 2
    lines = _sync_get_lines(db_path, target_id)
    assert len(lines) == 2
    texts = {line["text"] for line in lines}
    assert texts == {"first line", "second line"}


def test_merge_deletes_source(db_path):
    target_id = _sync_create_session(db_path, guild_id=GUILD_ID, channel_id=1)
    source_id = _sync_create_session(db_path, guild_id=GUILD_ID, channel_id=2)

    _sync_merge_sessions(db_path, target_id, [source_id], guild_id=GUILD_ID)

    assert _sync_get_session(db_path, source_id) is None


def test_target_still_exists_after_merge(db_path):
    target_id = _sync_create_session(db_path, guild_id=GUILD_ID, channel_id=1)
    source_id = _sync_create_session(db_path, guild_id=GUILD_ID, channel_id=2)

    _sync_merge_sessions(db_path, target_id, [source_id], guild_id=GUILD_ID)

    assert _sync_get_session(db_path, target_id) is not None


def test_merged_count(db_path):
    target_id = _sync_create_session(db_path, guild_id=GUILD_ID, channel_id=1)
    source_id = _sync_create_session(db_path, guild_id=GUILD_ID, channel_id=2)
    for i in range(5):
        _sync_add_line(db_path, source_id, i, f"Speaker{i}", f"line {i}", 0.9)

    result = _sync_merge_sessions(db_path, target_id, [source_id], guild_id=GUILD_ID)

    assert result["merged_count"] == 5


def test_invalid_source_skipped(db_path):
    target_id = _sync_create_session(db_path, guild_id=GUILD_ID, channel_id=1)
    fake_id = "19991231_235959"

    result = _sync_merge_sessions(db_path, target_id, [fake_id], guild_id=GUILD_ID)

    assert fake_id in result["skipped"]
    assert result["merged_count"] == 0


def test_target_wrong_guild_raises(db_path):
    target_id = _sync_create_session(db_path, guild_id=GUILD_ID, channel_id=1)

    with pytest.raises(ValueError):
        _sync_merge_sessions(db_path, target_id, [], guild_id=OTHER_GUILD_ID)


def test_source_wrong_guild_skipped(db_path):
    target_id = _sync_create_session(db_path, guild_id=GUILD_ID, channel_id=1)
    other_source = _sync_create_session(db_path, guild_id=OTHER_GUILD_ID, channel_id=2)
    _sync_add_line(db_path, other_source, 1, "Bob", "sneaky line", 0.7)

    result = _sync_merge_sessions(db_path, target_id, [other_source], guild_id=GUILD_ID)

    assert other_source in result["skipped"]
    assert result["merged_count"] == 0
    assert _sync_get_lines(db_path, target_id) == []


def test_multiple_sources(db_path):
    target_id = _sync_create_session(db_path, guild_id=GUILD_ID, channel_id=1)
    src1 = _sync_create_session(db_path, guild_id=GUILD_ID, channel_id=2)
    src2 = _sync_create_session(db_path, guild_id=GUILD_ID, channel_id=3)
    _sync_add_line(db_path, src1, 1, "Alice", "from src1", 0.9)
    _sync_add_line(db_path, src1, 1, "Alice", "also src1", 0.9)
    _sync_add_line(db_path, src2, 2, "Bob", "from src2", 0.8)

    result = _sync_merge_sessions(db_path, target_id, [src1, src2], guild_id=GUILD_ID)

    assert result["merged_count"] == 3
    assert result["skipped"] == []
    assert _sync_get_session(db_path, src1) is None
    assert _sync_get_session(db_path, src2) is None


def test_lines_accessible_after_merge(db_path):
    target_id = _sync_create_session(db_path, guild_id=GUILD_ID, channel_id=1)
    src1 = _sync_create_session(db_path, guild_id=GUILD_ID, channel_id=2)
    src2 = _sync_create_session(db_path, guild_id=GUILD_ID, channel_id=3)
    _sync_add_line(db_path, src1, 1, "Riven", "I search the room.", 0.95)
    _sync_add_line(db_path, src2, 2, "DM", "Roll perception.", 0.98)

    _sync_merge_sessions(db_path, target_id, [src1, src2], guild_id=GUILD_ID)

    lines = _sync_get_lines(db_path, target_id)
    texts = {line["text"] for line in lines}
    assert texts == {"I search the room.", "Roll perception."}
    assert all(line["session_id"] == target_id for line in lines)


def test_empty_source_ids(db_path):
    target_id = _sync_create_session(db_path, guild_id=GUILD_ID, channel_id=1)

    result = _sync_merge_sessions(db_path, target_id, [], guild_id=GUILD_ID)

    assert result["merged_count"] == 0
    assert result["skipped"] == []
