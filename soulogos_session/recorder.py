"""
Captures per-user voice audio from a Discord voice channel and feeds 2-second
chunks to an asyncio.Queue for transcription.

discord.py 2.x voice receive API:
  - Subclass discord.AudioSink and decorate on_audio with @AudioSink.listener()
  - VoiceClient.listen(sink) starts delivery; VoiceClient.stop_listening() stops it.
  - AudioFrame.data is raw stereo 16-bit PCM at 48 kHz (20 ms per frame = 3840 bytes).
"""

import asyncio

import discord

# 2 seconds of stereo 16-bit PCM at 48 kHz
_FLUSH_BYTES = 48_000 * 2 * 2 * 2
# Minimum retained on cleanup (>= 200 ms)
_MIN_FLUSH_BYTES = 48_000 * 2 * 2 // 5


class _TranscriptionSink(discord.AudioSink):
    def __init__(self, queue: asyncio.Queue) -> None:
        super().__init__()
        self._queue = queue
        self._buffers: dict[int, bytearray] = {}
        self._display: dict[int, str] = {}

    @discord.AudioSink.listener()
    def on_audio(self, member: discord.Member, frame: discord.AudioFrame) -> None:
        if member is None:
            return
        uid = member.id
        buf = self._buffers.setdefault(uid, bytearray())
        self._display[uid] = member.display_name
        buf.extend(frame.data)
        if len(buf) >= _FLUSH_BYTES:
            chunk, self._buffers[uid] = bytes(buf), bytearray()
            asyncio.create_task(
                self._queue.put((uid, member.display_name, chunk))
            )

    def cleanup(self) -> None:
        for uid, buf in self._buffers.items():
            if len(buf) >= _MIN_FLUSH_BYTES:
                name = self._display.get(uid, str(uid))
                asyncio.create_task(self._queue.put((uid, name, bytes(buf))))
        self._buffers.clear()


class Recorder:
    """Start/stop voice receive for a connected VoiceClient."""

    def __init__(self, voice_client: discord.VoiceClient, queue: asyncio.Queue) -> None:
        self._vc = voice_client
        self._sink = _TranscriptionSink(queue)

    def start(self) -> None:
        self._vc.listen(self._sink)

    def stop(self) -> None:
        self._vc.stop_listening()
