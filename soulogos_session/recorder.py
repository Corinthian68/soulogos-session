"""
Captures per-user voice audio from a Discord voice channel and feeds 2-second
chunks to an asyncio.Queue for transcription.

discord-ext-voice-recv API:
  - Subclass voice_recv.AudioSink and implement wants_opus(), write(), cleanup()
  - write() is called from a background thread; use run_coroutine_threadsafe.
  - DAVE E2EE audio is decrypted via vc.dave_decrypt before Opus decoding.
"""

import asyncio
import logging
import threading

import discord
import discord.opus
from davey import MediaType
from discord.ext import voice_recv

logger = logging.getLogger(__name__)
def _vc_dave_decrypt(self, usr_id, mtype, data):
    return self._connection.dave_session.decrypt(usr_id, mtype, data)
discord.VoiceClient.dave_decrypt = _vc_dave_decrypt
# 2 seconds of stereo 16-bit PCM at 48 kHz
_FLUSH_BYTES = 48_000 * 2 * 2 * 2
# Minimum retained on cleanup (>= 200 ms)
_MIN_FLUSH_BYTES = 48_000 * 2 * 2 // 5


class _TranscriptionSink(voice_recv.AudioSink):
    def __init__(self, vc: voice_recv.VoiceRecvClient, queue: asyncio.Queue) -> None:
        super().__init__()
        self._vc = vc
        self._queue = queue
        self._loop = asyncio.get_event_loop()
        self._buffers: dict[int, bytearray] = {}
        self._display: dict[int, str] = {}
        self._decoders: dict[int, discord.opus.Decoder] = {}
        self._lock = threading.Lock()
        # write() runs in a background thread; pause()/resume() are called from
        # the event loop. A threading.Event makes the flag safe across both.
        self._paused = threading.Event()

    def pause(self) -> None:
        self._paused.set()

    def resume(self) -> None:
        self._paused.clear()

    @property
    def is_paused(self) -> bool:
        return self._paused.is_set()

    def wants_opus(self) -> bool:
        return True

    def write(self, user: discord.Member, data: voice_recv.VoiceData) -> None:
        logger.debug("write() called: user=%s", getattr(user, "id", None))
        if user is None:
            return
        # Drop incoming audio while paused, before any decode/buffering work,
        # so nothing reaches the transcription queue.
        if self._paused.is_set():
            return
        opus_payload = getattr(data, "opus", None)
        if not opus_payload:
            return
        uid = user.id
        try:
            opus_bytes = self._vc.dave_decrypt(uid, MediaType.audio, bytes(opus_payload))
            logger.debug("dave_decrypt result: %s bytes", len(opus_bytes) if opus_bytes else 0)
        except Exception as exc:
            logger.warning("dave_decrypt failed for user %s: %s", uid, exc)
            # DAVE E2EE may not be active on this server; try decoding raw.
            opus_bytes = bytes(opus_payload)
        if not opus_bytes:
            return
        decoder = self._decoders.get(uid)
        if decoder is None:
            decoder = discord.opus.Decoder()
            self._decoders[uid] = decoder
        try:
            pcm = decoder.decode(opus_bytes, fec=False)
        except discord.opus.OpusError:
            return
        chunk = None
        with self._lock:
            self._display[uid] = user.display_name
            buf = self._buffers.setdefault(uid, bytearray())
            buf.extend(pcm)
            if len(buf) >= _FLUSH_BYTES:
                chunk = bytes(buf)
                self._buffers[uid] = bytearray()
        if chunk is not None:
            self._loop.call_soon_threadsafe(
                self._queue.put_nowait, (uid, user.display_name, chunk)
            )

    def cleanup(self) -> None:
        with self._lock:
            to_flush = [
                (uid, self._display.get(uid, str(uid)), bytes(buf))
                for uid, buf in self._buffers.items()
                if len(buf) >= _MIN_FLUSH_BYTES
            ]
            self._buffers.clear()
            self._decoders.clear()
        for uid, name, chunk in to_flush:
            self._loop.call_soon_threadsafe(
                self._queue.put_nowait, (uid, name, chunk)
            )


class Recorder:
    """Start/stop voice receive for a connected VoiceRecvClient."""

    def __init__(self, voice_client: voice_recv.VoiceRecvClient, queue: asyncio.Queue) -> None:
        self._vc = voice_client
        self._sink = _TranscriptionSink(voice_client, queue)

    def start(self) -> None:
        self._vc.listen(self._sink)

    def stop(self) -> None:
        self._vc.stop_listening()

    def pause(self) -> None:
        """Drop incoming audio packets without leaving the voice channel."""
        self._sink.pause()

    def resume(self) -> None:
        """Resume feeding incoming audio to the transcription queue."""
        self._sink.resume()

    @property
    def is_paused(self) -> bool:
        return self._sink.is_paused
