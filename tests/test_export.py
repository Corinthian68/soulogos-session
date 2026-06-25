from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from datetime import datetime, timezone

from soulogos_session.bot import (
    _chunk_by_words,
    _format_status_timestamp,
    _format_timestamp,
    _format_transcript_plain,
    _format_transcript_timed,
    _load_recap_prompt,
    _load_summary_prompt,
    _make_condense_callback,
    _make_export_callback,
    _make_recap_callback,
)
from soulogos_session.store import SessionStore

_DM_CHANNEL = 111
_PLAYER_CHANNEL = 222


_LINES = [
    {"timestamp": "2026-06-24T13:00:05+00:00", "display_name": "Thalindra", "text": "I cast fireball."},
    {"timestamp": "2026-06-24T13:01:42+00:00", "display_name": "DM", "text": "Roll for damage."},
]


# --- formatting helpers -----------------------------------------------------

def test_format_timestamp_iso() -> None:
    assert _format_timestamp("2026-06-24T13:00:05+00:00") == "13:00:05"


def test_format_timestamp_empty() -> None:
    assert _format_timestamp("") == "--:--:--"


def test_format_timestamp_bad_value_fallback() -> None:
    assert _format_timestamp("2026-06-24 09:08:07 garbage") == "09:08:07"


def test_format_status_timestamp_local_12h() -> None:
    # Compare against the same UTC instant converted to local time, so the test
    # is timezone-independent. Result must be "YYYY-MM-DD H:MM AM/PM" (no leading
    # zero on the hour, no seconds/microseconds/offset).
    ts = "2026-06-25T20:05:26.402869+00:00"
    local = datetime(2026, 6, 25, 20, 5, 26, 402869, tzinfo=timezone.utc).astimezone()
    hour = local.strftime("%I").lstrip("0") or "12"
    expected = f"{local.strftime('%Y-%m-%d')} {hour}:{local.strftime('%M %p')}"
    assert _format_status_timestamp(ts) == expected
    # Shape guard: no seconds, no microseconds, no timezone offset.
    assert ":26" not in _format_status_timestamp(ts)
    assert "+00:00" not in _format_status_timestamp(ts)
    assert _format_status_timestamp(ts).endswith(("AM", "PM"))


def test_format_status_timestamp_bad_value_fallback() -> None:
    assert _format_status_timestamp("not a timestamp") == "not a timestamp"


def test_format_transcript_plain_has_timestamps() -> None:
    out = _format_transcript_plain("20260624_130000", _LINES)
    assert out.startswith("# Session Transcript: 20260624_130000")
    assert "[13:00:05] **Thalindra:** I cast fireball." in out
    assert "[13:01:42] **DM:** Roll for damage." in out


def test_format_plain_with_name() -> None:
    out = _format_transcript_plain("20260624_130000", _LINES, "My Campaign")
    assert out.startswith("# My Campaign - Session Transcript: 20260624_130000")


def test_format_transcript_timed() -> None:
    out = _format_transcript_timed(_LINES)
    assert out == (
        "[13:00:05] Thalindra: I cast fireball.\n"
        "[13:01:42] DM: Roll for damage."
    )


# --- word chunker -----------------------------------------------------------

def test_chunk_by_words_under_limit() -> None:
    assert _chunk_by_words("one two three", max_words=10) == ["one two three"]


def test_chunk_by_words_splits() -> None:
    words = " ".join(str(i) for i in range(25))
    chunks = _chunk_by_words(words, max_words=10)
    assert len(chunks) == 3
    assert all(len(c.split()) <= 10 for c in chunks)
    # Reassembles to the original word sequence
    assert " ".join(chunks).split() == words.split()


def test_chunk_by_words_empty() -> None:
    assert _chunk_by_words("", max_words=10) == [""]


# --- summary prompt loading -------------------------------------------------

def test_load_summary_prompt_reads_file(tmp_path: Path) -> None:
    p = tmp_path / "prompt.txt"
    p.write_text("Custom condense prompt.", encoding="utf-8")
    assert _load_summary_prompt(p) == "Custom condense prompt."


def test_load_summary_prompt_missing_file_falls_back(tmp_path: Path) -> None:
    out = _load_summary_prompt(tmp_path / "does_not_exist.txt")
    assert "condensing" in out.lower()  # generic fallback


# --- shared test helpers ----------------------------------------------------

def _make_interaction() -> MagicMock:
    interaction = MagicMock()
    interaction.response = MagicMock()
    interaction.response.defer = AsyncMock()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()
    return interaction


def _make_bot(
    tmp_path: Path, lines, session, summary_prompt="PROMPT", recap_prompt="RECAP_PROMPT"
) -> tuple[MagicMock, MagicMock]:
    store = MagicMock()
    store.get_lines = AsyncMock(return_value=lines)
    store.get_session = AsyncMock(return_value=session)

    bot = MagicMock()
    bot.store = store
    bot.config = MagicMock()
    bot.config.summaries_path = tmp_path / "summaries"
    bot.config.logs_path = tmp_path / "logs"
    bot.config.anthropic_api_key = "test-key"
    bot.config.dm_channel_id = _DM_CHANNEL
    bot.config.player_channel_id = _PLAYER_CHANNEL
    bot.summary_prompt = summary_prompt
    bot.recap_prompt = recap_prompt

    channel = MagicMock()
    channel.send = AsyncMock()
    bot.get_channel = MagicMock(return_value=channel)
    return bot, channel


# --- export -----------------------------------------------------------------

async def test_export_no_lines(tmp_path: Path) -> None:
    bot, _ = _make_bot(tmp_path, [], {"id": "x", "name": ""})
    interaction = _make_interaction()
    await _make_export_callback(bot, "20260624_130000")(interaction)

    interaction.response.defer.assert_called_once_with(ephemeral=True)
    interaction.followup.send.assert_called_once()
    assert "No transcript lines found" in interaction.followup.send.call_args.args[0]


async def test_export_posts_file_to_channel(tmp_path: Path) -> None:
    bot, channel = _make_bot(tmp_path, _LINES, {"id": "20260624_130000", "name": ""})
    interaction = _make_interaction()

    with patch("soulogos_session.bot.discord.File") as mock_file:
        await _make_export_callback(bot, "20260624_130000")(interaction)

    out_path = tmp_path / "summaries" / "session_20260624_130000_transcript.md"
    assert out_path.exists()
    content = out_path.read_text(encoding="utf-8")
    assert content.startswith("# Session Transcript: 20260624_130000")
    assert "[13:00:05] **Thalindra:** I cast fireball." in content

    # A File is created for the DM and a fresh File for the channel (two total).
    assert mock_file.call_count == 2

    # Posted to the DM-only (prep-notes) channel, with the no-name header and a file kwarg.
    bot.get_channel.assert_called_once_with(_DM_CHANNEL)
    channel.send.assert_called_once()
    assert channel.send.call_args.args[0] == "📄 **Session 20260624_130000**"
    assert "file" in channel.send.call_args.kwargs

    # Final ephemeral confirmation
    assert "posted to #prep-notes" in interaction.followup.send.call_args.args[0]


async def test_export_channel_header_with_name(tmp_path: Path) -> None:
    bot, channel = _make_bot(
        tmp_path, _LINES, {"id": "20260624_130000", "name": "Crown S6"}
    )
    interaction = _make_interaction()

    with patch("soulogos_session.bot.discord.File"):
        await _make_export_callback(bot, "20260624_130000")(interaction)

    assert channel.send.call_args.args[0] == "📄 **Crown S6** - Session Transcript"
    out_path = tmp_path / "summaries" / "session_20260624_130000_transcript.md"
    assert out_path.read_text(encoding="utf-8").startswith(
        "# Crown S6 - Session Transcript: 20260624_130000"
    )


# --- condense ---------------------------------------------------------------

def _mock_anthropic(text: str) -> MagicMock:
    response = MagicMock()
    response.content = [MagicMock(text=text)]
    client = MagicMock()
    client.messages.create = AsyncMock(return_value=response)
    return client


async def test_condense_no_lines(tmp_path: Path) -> None:
    bot, _ = _make_bot(tmp_path, [], {"id": "x", "name": ""})
    interaction = _make_interaction()
    await _make_condense_callback(bot, "20260624_130000")(interaction)
    assert "No transcript lines found" in interaction.followup.send.call_args.args[0]


async def test_condense_short_single_call(tmp_path: Path) -> None:
    bot, channel = _make_bot(tmp_path, _LINES, {"id": "20260624_130000", "name": "Crown S6"})
    interaction = _make_interaction()
    client = _mock_anthropic("# Debrief\n- thing happened")

    with patch("soulogos_session.bot.anthropic.AsyncAnthropic", return_value=client), \
         patch("soulogos_session.bot.discord.File"):
        await _make_condense_callback(bot, "20260624_130000")(interaction)

    # Short transcript -> exactly one Claude call
    client.messages.create.assert_called_once()
    assert client.messages.create.call_args.kwargs["model"] == "claude-sonnet-4-6"
    assert client.messages.create.call_args.kwargs["max_tokens"] == 2048
    assert client.messages.create.call_args.kwargs["system"] == "PROMPT"

    # Structured log stored under data/logs/, user-facing wording unchanged.
    out_path = tmp_path / "logs" / "session_20260624_130000_structured.md"
    assert out_path.exists()
    assert out_path.read_text(encoding="utf-8") == "# Debrief\n- thing happened"

    bot.get_channel.assert_called_once_with(_DM_CHANNEL)
    assert channel.send.call_args.args[0] == "🎲 **Crown S6** - Session Debrief"
    assert "Debrief generated and posted to #prep-notes." == interaction.followup.send.call_args.args[0]


async def test_condense_long_chunks_and_stitches(tmp_path: Path) -> None:
    # Build a transcript well over 3000 words.
    long_lines = [
        {
            "timestamp": "2026-06-24T13:00:00+00:00",
            "display_name": "Player",
            "text": " ".join(["word"] * 50),
        }
        for _ in range(100)  # ~5000+ words total
    ]
    bot, channel = _make_bot(tmp_path, long_lines, {"id": "20260624_130000", "name": ""})
    interaction = _make_interaction()
    client = _mock_anthropic("section/stitched debrief")

    with patch("soulogos_session.bot.anthropic.AsyncAnthropic", return_value=client), \
         patch("soulogos_session.bot.discord.File"):
        await _make_condense_callback(bot, "20260624_130000")(interaction)

    # More than one call: N section calls + 1 stitch call
    assert client.messages.create.call_count >= 3

    out_path = tmp_path / "logs" / "session_20260624_130000_structured.md"
    assert out_path.exists()
    assert channel.send.call_args.args[0] == "🎲 **Session 20260624_130000**"


async def test_condense_api_error(tmp_path: Path) -> None:
    bot, _ = _make_bot(tmp_path, _LINES, {"id": "20260624_130000", "name": ""})
    interaction = _make_interaction()
    client = MagicMock()
    client.messages.create = AsyncMock(side_effect=Exception("API down"))

    with patch("soulogos_session.bot.anthropic.AsyncAnthropic", return_value=client):
        await _make_condense_callback(bot, "20260624_130000")(interaction)

    assert "Failed to generate debrief" in interaction.followup.send.call_args.args[0]


# --- recap ------------------------------------------------------------------

async def test_recap_no_lines(tmp_path: Path) -> None:
    bot, _ = _make_bot(tmp_path, [], {"id": "x", "name": ""})
    interaction = _make_interaction()
    await _make_recap_callback(bot, "20260624_130000")(interaction)
    assert "No transcript lines found" in interaction.followup.send.call_args.args[0]


def _write_structured_log(tmp_path: Path, session_id: str, content: str) -> Path:
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    path = logs_dir / f"session_{session_id}_structured.md"
    path.write_text(content, encoding="utf-8")
    return path


async def test_recap_reads_existing_structured_log(tmp_path: Path) -> None:
    # A structured log already exists -> recap must read IT, not the raw transcript.
    _write_structured_log(tmp_path, "20260624_130000", "STRUCTURED LOG BODY")
    bot, channel = _make_bot(tmp_path, _LINES, {"id": "20260624_130000", "name": "Crown S6"})
    interaction = _make_interaction()
    client = _mock_anthropic("The party did things.")

    with patch("soulogos_session.bot.anthropic.AsyncAnthropic", return_value=client), \
         patch("soulogos_session.bot.discord.File"):
        await _make_recap_callback(bot, "20260624_130000")(interaction)

    # Exactly one Claude call (the recap) -- no condense call, since the
    # structured log already existed.
    client.messages.create.assert_called_once()
    assert client.messages.create.call_args.kwargs["max_tokens"] == 1024
    assert client.messages.create.call_args.kwargs["system"] == "RECAP_PROMPT"
    assert client.messages.create.call_args.kwargs["model"] == "claude-sonnet-4-6"

    # The recap's input IS the structured log file contents -- not the transcript.
    sent_content = client.messages.create.call_args.kwargs["messages"][0]["content"]
    assert sent_content == "STRUCTURED LOG BODY"
    assert "fireball" not in sent_content  # raw transcript text must not leak in

    # Reading the existing log must not touch the raw transcript at all.
    bot.store.get_lines.assert_not_called()

    out_path = tmp_path / "summaries" / "session_20260624_130000_recap.md"
    assert out_path.exists()
    assert out_path.read_text(encoding="utf-8") == "The party did things."

    # Posted to the player-facing (session-log) channel.
    bot.get_channel.assert_called_once_with(_PLAYER_CHANNEL)
    assert channel.send.call_args.args[0] == "📜 **Crown S6** - Session Recap"
    assert "Recap generated and posted to #session-log." == interaction.followup.send.call_args.args[0]


async def test_recap_creates_structured_log_when_missing(tmp_path: Path) -> None:
    # No structured log yet -> recap silently builds one from the transcript
    # (NOT posted to prep-notes), then writes the recap from that structured log.
    bot, channel = _make_bot(tmp_path, _LINES, {"id": "20260624_130000", "name": ""})
    interaction = _make_interaction()
    client = _mock_anthropic("generated text")

    with patch("soulogos_session.bot.anthropic.AsyncAnthropic", return_value=client), \
         patch("soulogos_session.bot.discord.File"):
        await _make_recap_callback(bot, "20260624_130000")(interaction)

    # Two Claude calls: condense (build structured log) then recap.
    assert client.messages.create.call_count == 2
    systems = [c.kwargs["system"] for c in client.messages.create.call_args_list]
    assert systems == ["PROMPT", "RECAP_PROMPT"]

    # Structured log created on disk (silently); recap also written.
    structured_path = tmp_path / "logs" / "session_20260624_130000_structured.md"
    assert structured_path.exists()
    recap_path = tmp_path / "summaries" / "session_20260624_130000_recap.md"
    assert recap_path.exists()

    # Only the recap is posted (to session-log); structured-log creation is silent.
    bot.get_channel.assert_called_once_with(_PLAYER_CHANNEL)
    assert channel.send.call_args.args[0] == "📜 **Session 20260624_130000**"


async def test_recap_recap_input_is_structured_not_raw(tmp_path: Path) -> None:
    # End-to-end guard: when recap builds the log itself, the recap call's input
    # is the structured log text (the condense output), never the raw transcript.
    bot, _ = _make_bot(tmp_path, _LINES, {"id": "20260624_130000", "name": ""})
    interaction = _make_interaction()
    client = _mock_anthropic("DEBRIEF FROM TRANSCRIPT")

    with patch("soulogos_session.bot.anthropic.AsyncAnthropic", return_value=client), \
         patch("soulogos_session.bot.discord.File"):
        await _make_recap_callback(bot, "20260624_130000")(interaction)

    # Second call is the recap; its user content is the structured log produced
    # by the first (condense) call -- here the mock returns "DEBRIEF FROM TRANSCRIPT".
    recap_call = client.messages.create.call_args_list[1]
    assert recap_call.kwargs["system"] == "RECAP_PROMPT"
    assert recap_call.kwargs["messages"][0]["content"] == "DEBRIEF FROM TRANSCRIPT"
    assert "fireball" not in recap_call.kwargs["messages"][0]["content"]


def test_load_recap_prompt_falls_back(tmp_path: Path) -> None:
    out = _load_recap_prompt(tmp_path / "missing.txt")
    assert "recap" in out.lower()


# --- config -----------------------------------------------------------------

def test_config_has_channel_and_prompt_defaults() -> None:
    import os
    from soulogos_session.config import load_config

    os.environ["DISCORD_BOT_TOKEN"] = "x"
    cfg = load_config()
    assert cfg.dm_channel_id == 1499171448043081911
    assert cfg.player_channel_id == 1499170547601506355
    assert str(cfg.summary_prompt_path) == "data/prompts/crown_summary_prompt.txt"
    assert str(cfg.recap_prompt_path) == "data/prompts/crown_recap_prompt.txt"


# --- store: delete-all and name (unchanged behavior) ------------------------

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
    await store.create_session(guild_id=111, channel_id=1)
    sid_b = await store.create_session(guild_id=222, channel_id=2)

    deleted = await store.delete_all_sessions(guild_id=111)
    assert deleted == 1
    assert await store.list_sessions(guild_id=111) == []
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
