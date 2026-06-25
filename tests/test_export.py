from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from soulogos_session.bot import (
    _format_timestamp,
    _format_transcript_plain,
    _format_transcript_fancy,
    _make_export_callback,
)
from soulogos_session.store import SessionStore


_LINES = [
    {"timestamp": "2026-06-24T13:00:05+00:00", "display_name": "Thalindra", "text": "I cast fireball."},
    {"timestamp": "2026-06-24T13:01:42+00:00", "display_name": "DM", "text": "Roll for damage."},
]


def test_format_timestamp_iso() -> None:
    assert _format_timestamp("2026-06-24T13:00:05+00:00") == "13:00:05"


def test_format_timestamp_empty() -> None:
    assert _format_timestamp("") == "--:--:--"


def test_format_timestamp_bad_value_fallback() -> None:
    # Unparseable but long enough to slice the time portion out.
    assert _format_timestamp("2026-06-24 09:08:07 garbage") == "09:08:07"


def test_format_transcript_plain_has_timestamps() -> None:
    out = _format_transcript_plain("20260624_130000", _LINES)
    assert out.startswith("# Session Transcript: 20260624_130000")
    assert "[13:00:05] **Thalindra:** I cast fireball." in out
    assert "[13:01:42] **DM:** Roll for damage." in out


def test_format_transcript_fancy_has_timestamps() -> None:
    out = _format_transcript_fancy("20260624_130000", _LINES)
    assert out.startswith("## 🎲 Session Transcript: 20260624_130000")
    assert "🗣️ `[13:00:05]` **Thalindra:** I cast fireball." in out
    assert "*Transcribed by Soulogos Session*" in out


def _make_interaction() -> MagicMock:
    interaction = MagicMock()
    interaction.response = MagicMock()
    interaction.response.defer = AsyncMock()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()
    return interaction


async def test_export_no_lines(tmp_path: Path) -> None:
    store = MagicMock()
    store.get_lines = AsyncMock(return_value=[])
    bot = MagicMock()
    bot.store = store
    bot.config = MagicMock()
    bot.config.summaries_path = tmp_path / "summaries"

    interaction = _make_interaction()
    callback = _make_export_callback(bot, "20260624_130000")
    await callback(interaction)

    interaction.response.defer.assert_called_once_with(ephemeral=True)
    interaction.followup.send.assert_called_once()
    assert "No transcript lines found" in interaction.followup.send.call_args.args[0]


async def test_export_success(tmp_path: Path) -> None:
    store = MagicMock()
    store.get_lines = AsyncMock(return_value=_LINES)
    store.get_session = AsyncMock(return_value={"id": "20260624_130000", "name": ""})
    bot = MagicMock()
    bot.store = store
    bot.config = MagicMock()
    bot.config.summaries_path = tmp_path / "summaries"

    channel = MagicMock()
    channel.send = AsyncMock()
    bot.get_channel = MagicMock(return_value=channel)

    interaction = _make_interaction()

    with patch("soulogos_session.bot.discord.File") as mock_file:
        callback = _make_export_callback(bot, "20260624_130000")
        await callback(interaction)

    # Plain transcript written to disk
    out_path = tmp_path / "summaries" / "session_20260624_130000_transcript.md"
    assert out_path.exists()
    content = out_path.read_text(encoding="utf-8")
    assert content.startswith("# Session Transcript: 20260624_130000")
    assert "[13:00:05] **Thalindra:** I cast fireball." in content

    # File posted to the DM, fancy version posted to the channel
    mock_file.assert_called_once()
    channel.send.assert_called_once()
    fancy = channel.send.call_args.args[0]
    assert "🗣️ `[13:00:05]`" in fancy

    # Confirmation followup
    assert interaction.followup.send.call_count == 2
    assert "posted to #session-log" in interaction.followup.send.call_args.args[0]


async def test_export_success_with_name(tmp_path: Path) -> None:
    store = MagicMock()
    store.get_lines = AsyncMock(return_value=_LINES)
    store.get_session = AsyncMock(
        return_value={"id": "20260624_130000", "name": "Crown of the Oathbreaker S6"}
    )
    bot = MagicMock()
    bot.store = store
    bot.config = MagicMock()
    bot.config.summaries_path = tmp_path / "summaries"

    channel = MagicMock()
    channel.send = AsyncMock()
    bot.get_channel = MagicMock(return_value=channel)

    interaction = _make_interaction()

    with patch("soulogos_session.bot.discord.File"):
        callback = _make_export_callback(bot, "20260624_130000")
        await callback(interaction)

    out_path = tmp_path / "summaries" / "session_20260624_130000_transcript.md"
    content = out_path.read_text(encoding="utf-8")
    assert content.startswith("# Crown of the Oathbreaker S6 - Session Transcript: 20260624_130000")

    fancy = channel.send.call_args.args[0]
    assert fancy.startswith("## 🎲 Crown of the Oathbreaker S6 - Session Transcript: 20260624_130000")


def test_format_plain_with_name() -> None:
    out = _format_transcript_plain("20260624_130000", _LINES, "My Campaign")
    assert out.startswith("# My Campaign - Session Transcript: 20260624_130000")


def test_format_fancy_with_name() -> None:
    out = _format_transcript_fancy("20260624_130000", _LINES, "My Campaign")
    assert out.startswith("## 🎲 My Campaign - Session Transcript: 20260624_130000")


@pytest.fixture
async def store(tmp_path: Path) -> SessionStore:
    s = SessionStore(tmp_path / "test.sqlite")
    await s.init()
    return s


async def test_delete_all_sessions(store: SessionStore) -> None:
    sid1 = await store.create_session(guild_id=111, channel_id=1)
    sid2 = await store.create_session(guild_id=111, channel_id=2)
    await store.add_line(session_id=sid1, discord_user_id=1, display_name="A", text="x", confidence=1.0)
    await store.add_line(session_id=sid2, discord_user_id=2, display_name="B", text="y", confidence=1.0)

    deleted = await store.delete_all_sessions(guild_id=111)
    assert deleted == 2
    assert await store.list_sessions(guild_id=111) == []
    assert await store.get_lines(sid1) == []
    assert await store.get_lines(sid2) == []


async def test_delete_all_sessions_guild_scoped(store: SessionStore) -> None:
    sid_a = await store.create_session(guild_id=111, channel_id=1)
    sid_b = await store.create_session(guild_id=222, channel_id=2)

    deleted = await store.delete_all_sessions(guild_id=111)
    assert deleted == 1
    assert await store.list_sessions(guild_id=111) == []
    # Other guild untouched
    remaining = await store.list_sessions(guild_id=222)
    assert len(remaining) == 1
    assert remaining[0]["id"] == sid_b


async def test_delete_all_sessions_empty(store: SessionStore) -> None:
    assert await store.delete_all_sessions(guild_id=999) == 0


async def test_create_session_with_name(store: SessionStore) -> None:
    sid = await store.create_session(guild_id=111, channel_id=1, name="Crown of the Oathbreaker S6")
    session = await store.get_session(sid)
    assert session is not None
    assert session["name"] == "Crown of the Oathbreaker S6"


async def test_create_session_without_name(store: SessionStore) -> None:
    sid = await store.create_session(guild_id=111, channel_id=1)
    session = await store.get_session(sid)
    assert session is not None
    assert (session["name"] or "") == ""


async def test_get_session_not_found(store: SessionStore) -> None:
    assert await store.get_session("19990101_000000") is None


async def test_list_sessions_includes_name(store: SessionStore) -> None:
    sid = await store.create_session(guild_id=111, channel_id=1, name="Named Session")
    sessions = await store.list_sessions(guild_id=111)
    assert len(sessions) == 1
    assert sessions[0]["id"] == sid
    assert sessions[0]["name"] == "Named Session"
