"""Standalone check of Recorder/_TranscriptionSink pause behavior.

Run outside pytest so the REAL discord.ext.voice_recv is used (pytest's
conftest stubs it with a MagicMock, which breaks subclassing).
"""
import asyncio
from unittest.mock import MagicMock

from soulogos_session.recorder import Recorder, _TranscriptionSink


async def main() -> None:
    # Recorder flag toggling
    rec = Recorder(MagicMock(), asyncio.Queue())
    assert rec.is_paused is False, "should start un-paused"
    rec.pause()
    assert rec.is_paused is True, "pause() should set the flag"
    rec.resume()
    assert rec.is_paused is False, "resume() should clear the flag"

    # Sink drops audio while paused (returns before touching the queue)
    queue: asyncio.Queue = asyncio.Queue()
    sink = _TranscriptionSink(MagicMock(), queue)
    sink.pause()
    user = MagicMock(); user.id = 42
    data = MagicMock(); data.opus = b"\x00\x01\x02"
    sink.write(user, data)
    assert queue.empty(), "paused sink must drop packets before queueing"

    print("OK: recorder pause/resume + paused-drop verified")


asyncio.run(main())
