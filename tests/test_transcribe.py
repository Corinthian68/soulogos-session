from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from soulogos_session.bot import _format_transcript, _make_transcribe_callback


def _make_bot(tmp_path: Path, lines=None, api_key: str = "test-key") -> MagicMock:
    store = MagicMock()
    store.get_lines = AsyncMock(return_value=lines if lines is not None else [])

    config = MagicMock()
    config.anthropic_api_key = api_key
    config.summaries_path = tmp_path / "summaries"

    bot = MagicMock()
    bot.store = store
    bot.config = config
    return bot


def _make_interaction() -> MagicMock:
    interaction = MagicMock()
    interaction.response = MagicMock()
    interaction.response.defer = AsyncMock()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()
    return interaction


async def test_format_transcript_basic() -> None:
    lines = [
        {"timestamp": "2026-06-24T13:00:00Z", "display_name": "Thalindra", "text": "I cast fireball."},
        {"timestamp": "2026-06-24T13:00:10Z", "display_name": "DM", "text": "Roll damage."},
    ]
    result = _format_transcript(lines)
    assert "[2026-06-24T13:00:00Z] Thalindra: I cast fireball." in result
    assert "[2026-06-24T13:00:10Z] DM: Roll damage." in result


async def test_format_transcript_empty() -> None:
    assert _format_transcript([]) == ""


async def test_transcribe_no_lines(tmp_path: Path) -> None:
    bot = _make_bot(tmp_path, lines=[])
    interaction = _make_interaction()

    callback = _make_transcribe_callback(bot, "20260624_130000")
    await callback(interaction)

    interaction.response.defer.assert_called_once_with(ephemeral=True)
    interaction.followup.send.assert_called_once()
    sent = interaction.followup.send.call_args.args[0]
    assert "No transcript lines" in sent
    assert "20260624_130000" in sent


async def test_transcribe_success(tmp_path: Path) -> None:
    lines = [
        {"timestamp": "2026-06-24T13:00:00Z", "display_name": "Thalindra", "text": "I cast fireball."},
        {"timestamp": "2026-06-24T13:00:15Z", "display_name": "DM", "text": "Roll for damage."},
    ]
    bot = _make_bot(tmp_path, lines=lines)
    interaction = _make_interaction()

    mock_response = MagicMock()
    mock_response.content = [MagicMock(text="# Session Summary\n\nA battle occurred.")]

    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(return_value=mock_response)
    bot.store.get_session = AsyncMock(return_value={"name": "Test Session"})
    mock_channel = AsyncMock()
    bot.get_channel = MagicMock(return_value=mock_channel)

    with (
        patch("soulogos_session.bot.anthropic.AsyncAnthropic", return_value=mock_client),
        patch("soulogos_session.bot.discord.File") as mock_file,
    ):
        callback = _make_transcribe_callback(bot, "20260624_130000")
        await callback(interaction)

    # Summary written to disk
    summary_path = tmp_path / "summaries" / "session_20260624_130000_summary.md"
    assert summary_path.exists()
    content = summary_path.read_text(encoding="utf-8")
    assert "Session Summary" in content

    # Anthropic called with correct model
    mock_client.messages.create.assert_called_once()
    call_kwargs = mock_client.messages.create.call_args.kwargs
    assert call_kwargs["model"] == "claude-sonnet-4-6"

    # discord.File created pointing at the summary file
    mock_file.assert_called_once()
    assert "session_20260624_130000_summary.md" in mock_file.call_args.args[0]

    # Followup sent with file
    interaction.followup.send.assert_called_once()


async def test_transcribe_api_error(tmp_path: Path) -> None:
    lines = [
        {"timestamp": "2026-06-24T13:00:00Z", "display_name": "Thalindra", "text": "I attack."},
    ]
    bot = _make_bot(tmp_path, lines=lines)
    interaction = _make_interaction()

    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(side_effect=Exception("API unavailable"))

    with patch("soulogos_session.bot.anthropic.AsyncAnthropic", return_value=mock_client):
        callback = _make_transcribe_callback(bot, "20260624_130000")
        await callback(interaction)

    interaction.response.defer.assert_called_once_with(ephemeral=True)
    interaction.followup.send.assert_called_once()
    sent = interaction.followup.send.call_args.args[0]
    assert "Failed" in sent
