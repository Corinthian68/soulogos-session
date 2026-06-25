from unittest.mock import AsyncMock, MagicMock

from soulogos_session.bot import _RecordingControlView, _active_recorder

# NOTE: Recorder / _TranscriptionSink are exercised in scripts/verify_recorder.py
# rather than here: conftest.py stubs `discord.ext.voice_recv` with a MagicMock,
# and subclassing a MagicMock breaks attribute/property resolution, so the sink
# cannot be meaningfully instantiated under pytest. The view tests below assert
# the integration contract (that pause/resume calls reach the recorder).


def _make_interaction() -> MagicMock:
    interaction = MagicMock()
    interaction.response = MagicMock()
    interaction.response.edit_message = AsyncMock()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()
    return interaction


# --- _active_recorder helper -------------------------------------------------

def test_active_recorder_present() -> None:
    recorder = MagicMock()
    bot = MagicMock()
    bot._active = {7: ("sid", recorder, MagicMock())}
    assert _active_recorder(bot, 7) is recorder


def test_active_recorder_absent() -> None:
    bot = MagicMock()
    bot._active = {}
    assert _active_recorder(bot, 7) is None


# --- _RecordingControlView ---------------------------------------------------

def test_control_view_initial_button_states() -> None:
    bot = MagicMock()
    bot._active = {}
    view = _RecordingControlView(bot, 7)
    assert view.pause_btn.disabled is False
    assert view.resume_btn.disabled is True


async def test_control_view_pause_press() -> None:
    recorder = MagicMock()
    bot = MagicMock()
    bot._active = {7: ("sid", recorder, MagicMock())}
    view = _RecordingControlView(bot, 7)
    interaction = _make_interaction()

    await view._on_pause(interaction)

    recorder.pause.assert_called_once()
    assert view.pause_btn.disabled is True
    assert view.resume_btn.disabled is False
    interaction.response.edit_message.assert_called_once()
    assert interaction.response.edit_message.call_args.kwargs["view"] is view
    sent = interaction.followup.send.call_args.args[0]
    assert "paused" in sent.lower()
    assert interaction.followup.send.call_args.kwargs["ephemeral"] is True


async def test_control_view_resume_press() -> None:
    recorder = MagicMock()
    bot = MagicMock()
    bot._active = {7: ("sid", recorder, MagicMock())}
    view = _RecordingControlView(bot, 7, paused=True)
    interaction = _make_interaction()

    await view._on_resume(interaction)

    recorder.resume.assert_called_once()
    assert view.pause_btn.disabled is False
    assert view.resume_btn.disabled is True
    sent = interaction.followup.send.call_args.args[0]
    assert "resumed" in sent.lower()


async def test_control_view_no_active_session() -> None:
    bot = MagicMock()
    bot._active = {}
    view = _RecordingControlView(bot, 7)
    interaction = _make_interaction()

    await view._on_pause(interaction)

    # Both buttons disabled, user told there is no session.
    assert view.pause_btn.disabled is True
    assert view.resume_btn.disabled is True
    interaction.response.edit_message.assert_called_once()
    sent = interaction.followup.send.call_args.args[0]
    assert "No active session" in sent
    assert interaction.followup.send.call_args.kwargs["ephemeral"] is True
