import asyncio
import logging
import re
import sys
from datetime import datetime

import anthropic
import discord
from discord import app_commands
from discord.ext import voice_recv

from .config import Config
from .player_lookup import load_player_map, character_name
from .recorder import Recorder
from .store import SessionStore
from .transcriber import Transcriber

log = logging.getLogger(__name__)

# Fallbacks used when a configured prompt file cannot be read.
_GENERIC_SUMMARY_PROMPT = (
    "You are condensing a raw tabletop RPG session transcript into a structured "
    "debrief for the Dungeon Master. Produce a tight, bulleted markdown summary "
    "covering what happened, decisions made, NPC changes, open threads, resolved "
    "threads, notable items and resources, and what to carry into next session. "
    "No flavor language. Flag anything uncertain with [VERIFY]."
)

_GENERIC_RECAP_PROMPT = (
    "You are writing a brief player-facing recap of a tabletop RPG session, in "
    "second person ('The party...'). Cover what happened in order, key decisions, "
    "what the party learned, where the session ended, and one sentence of forward "
    "momentum. Exclude DM-only information and mechanical detail. 200-300 words of "
    "flowing prose, no headers or bullet points."
)


def _load_prompt(path, fallback: str) -> str:
    """Load prompt text from a file, falling back to a generic prompt."""
    try:
        text = path.read_text(encoding="utf-8").strip()
        if text:
            return text
        log.warning("Prompt file %s is empty; using generic prompt.", path)
    except OSError as exc:
        log.warning("Could not read prompt %s (%s); using generic prompt.", path, exc)
    return fallback


def _load_summary_prompt(path) -> str:
    """Load the condense prompt text, falling back to a generic prompt."""
    return _load_prompt(path, _GENERIC_SUMMARY_PROMPT)


def _load_recap_prompt(path) -> str:
    """Load the player recap prompt text, falling back to a generic prompt."""
    return _load_prompt(path, _GENERIC_RECAP_PROMPT)


_HALLUCINATION_PATTERNS = [
    re.compile(r"^\s*[.!?,;:…]+\s*$"),
    re.compile(r"^(thank you for watching|thanks for watching)", re.IGNORECASE),
    re.compile(r"^(don't forget to like|please like and subscribe|like share and subscribe)", re.IGNORECASE),
    re.compile(r"^(subscribe|like and subscribe|please subscribe)", re.IGNORECASE),
    re.compile(r"^(d&d|dungeons and dragons|ttrpg|dungeon master)[,\s]", re.IGNORECASE),
    re.compile(r"^(okay|ok|um+|uh+|hmm+)\s*$", re.IGNORECASE),
    re.compile(r"^bye[\s.!]*$", re.IGNORECASE),
    re.compile(r"(thank you for watching).*(like|share|subscribe)", re.IGNORECASE),
]


def _is_hallucination(text: str) -> bool:
    t = text.strip()
    if not t:
        return True
    for pat in _HALLUCINATION_PATTERNS:
        if pat.search(t):
            return True
    return False


def _clean_lines_for_llm(lines: list[dict]) -> list[dict]:
    """Remove hallucination lines before sending transcript to LLM."""
    return [
        line for line in lines
        if line.get("text") and not _is_hallucination(line["text"])
    ]


class SoulogosBot(discord.Client):
    def __init__(self, config: Config) -> None:
        intents = discord.Intents.default()
        intents.voice_states = True
        intents.guilds = True
        super().__init__(intents=intents)

        self.config = config
        self.tree = app_commands.CommandTree(self)
        self.store = SessionStore(config.session_db_path)
        self.transcriber = Transcriber(config.whisper_model, config.whisper_device)
        self.summary_prompt = _load_summary_prompt(config.summary_prompt_path)
        self.recap_prompt = _load_recap_prompt(config.recap_prompt_path)

        # Active sessions: guild_id -> (session_id, Recorder, asyncio.Task)
        self._active: dict[int, tuple[str, Recorder, asyncio.Task]] = {}
        # Last voice channel the bot joined per guild: guild_id -> channel_id.
        # Used to auto-rejoin after an unexpected voice WebSocket drop.
        self._last_channel: dict[int, int] = {}
        # Guilds whose voice connection is being torn down intentionally
        # (via /session-end). Suppresses auto-rejoin for those disconnects.
        self._ending: set[int] = set()
        # Guilds with an auto-rejoin already in flight, so overlapping
        # VOICE_STATE_UPDATE events and watchdog ticks don't spawn duplicate
        # rejoin attempts.
        self._rejoining: set[int] = set()
        # Background reconciliation task; see _watchdog_loop.
        self._watchdog_task: asyncio.Task | None = None

        _register_commands(self)

    async def setup_hook(self) -> None:
        await self.store.init()
        guild = discord.Object(id=1433893663322149067)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)
        log.info("Commands synced.")

    async def on_ready(self) -> None:
        log.info("Logged in as %s (id=%s)", self.user, self.user.id)  # type: ignore[union-attr]
        # on_ready fires again after every gateway reconnect; only ever run one
        # watchdog at a time.
        if self._watchdog_task is None or self._watchdog_task.done():
            self._watchdog_task = asyncio.create_task(self._watchdog_loop())
            log.info("Voice watchdog started.")

    async def _watchdog_loop(self) -> None:
        """Reconcile active sessions with the live voice connection every 30s.

        on_voice_state_update is the fast path for clean disconnects, but when
        the gateway itself drops (WebSocket 1006 / RESUME) voice state events
        are not delivered, so a dead voice connection would otherwise go
        unnoticed and transcription would stop silently. This loop compares the
        desired state (an active session => connected to voice) against reality
        and triggers a rejoin when they diverge. It must never raise: any error
        is logged and the loop continues on the next tick.
        """
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                await asyncio.sleep(30)
                for guild_id in list(self._active.keys()):
                    # Intentional teardown or an in-flight rejoin: leave it be.
                    if guild_id in self._ending or guild_id in self._rejoining:
                        continue
                    channel_id = self._last_channel.get(guild_id)
                    if channel_id is None:
                        continue
                    guild = self.get_guild(guild_id)
                    if guild is None:
                        continue
                    vc = guild.voice_client
                    if (
                        isinstance(vc, voice_recv.VoiceRecvClient)
                        and vc.is_connected()
                    ):
                        continue  # Still connected; nothing to do.
                    log.warning(
                        "watchdog: active session in guild %s but voice is not "
                        "connected; attempting rejoin of channel %s",
                        guild_id,
                        channel_id,
                    )
                    self._rejoining.add(guild_id)
                    try:
                        await self._auto_rejoin(guild, channel_id)
                    finally:
                        self._rejoining.discard(guild_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("watchdog loop iteration failed; continuing")

    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        """Detect unexpected voice disconnects and auto-rejoin.

        Discord occasionally drops the voice WebSocket (close code 1006). The
        gateway reconnects on its own, but the voice connection is not restored,
        so transcription stops silently. When the bot itself is dropped from a
        voice channel mid-session -- and /session-end was not the cause -- we
        rejoin the same channel and resume the active session.
        """
        # Only react to the bot's own state changes, and only to being dropped
        # from a channel (before set, after cleared) -- not joins or moves.
        if self.user is None or member.id != self.user.id:
            return
        if before.channel is None or after.channel is not None:
            return

        guild_id = member.guild.id
        # Intentional teardown via /session-end: nothing to recover.
        if guild_id in self._ending:
            return
        # No active session means there's nothing to resume.
        if guild_id not in self._active:
            return
        # An auto-rejoin is already running for this guild.
        if guild_id in self._rejoining:
            return

        self._rejoining.add(guild_id)
        try:
            await self._auto_rejoin(member.guild, before.channel.id)
        finally:
            self._rejoining.discard(guild_id)

    async def _auto_rejoin(self, guild: discord.Guild, channel_id: int) -> None:
        """Rejoin ``channel_id`` and reattach the recorder to the live session.

        Retries up to 3 times with a 2-second wait before each attempt. On
        success logs a WARNING; if every attempt fails, logs an ERROR and marks
        the session ended.
        """
        entry = self._active.get(guild.id)
        if entry is None:
            return
        session_id, recorder, task = entry

        channel = guild.get_channel(channel_id)
        if not isinstance(channel, discord.VoiceChannel):
            log.error(
                "auto-rejoin: voice channel %s in guild %s is gone; ending session %s",
                channel_id,
                guild.id,
                session_id,
            )
            await self._mark_session_ended(guild.id)
            return

        for attempt in range(1, 4):
            await asyncio.sleep(2)
            # The session may have ended (e.g. /session-end) while we waited.
            if guild.id not in self._active:
                return
            # On a gateway drop the dead voice client often stays attached to
            # the guild; channel.connect() raises "Already connected" while it
            # lingers, so force it loose first. (Clean disconnects leave
            # voice_client as None, making this a no-op.)
            stale = guild.voice_client
            if stale is not None and not stale.is_connected():
                try:
                    await stale.disconnect(force=True)
                except Exception:
                    log.debug("auto-rejoin: stale voice client cleanup failed", exc_info=True)
            try:
                vc = await asyncio.wait_for(
                    channel.connect(cls=voice_recv.VoiceRecvClient),
                    timeout=10.0,
                )
            except Exception:
                # As in /session-join, connect() can settle the handshake yet
                # never return; adopt the live client if discord.py attached one.
                recovered = guild.voice_client
                if (
                    isinstance(recovered, voice_recv.VoiceRecvClient)
                    and recovered.is_connected()
                ):
                    vc = recovered
                else:
                    log.warning(
                        "auto-rejoin attempt %d/3 failed for channel %s (session %s)",
                        attempt,
                        channel.name,
                        session_id,
                        exc_info=True,
                    )
                    continue

            # Reattach a fresh recorder to the new client, reusing the original
            # queue so the transcription task and session continue uninterrupted.
            # Preserve the pause state across the reconnect.
            was_paused = recorder.is_paused
            try:
                recorder.stop()
            except Exception:
                # The old recorder's voice client is dead; stop is best-effort.
                pass
            new_recorder = Recorder(vc, recorder.queue)
            if was_paused:
                new_recorder.pause()
            new_recorder.start()
            self._active[guild.id] = (session_id, new_recorder, task)
            self._last_channel[guild.id] = channel.id
            log.warning(
                "auto-rejoined voice channel %s after unexpected disconnect "
                "(attempt %d/3); session %s resumed",
                channel.name,
                attempt,
                session_id,
            )
            return

        log.error(
            "auto-rejoin failed after 3 attempts for channel %s; ending session %s",
            channel.name,
            session_id,
        )
        await self._mark_session_ended(guild.id)

    async def _mark_session_ended(self, guild_id: int) -> None:
        """Tear down the active session for a guild (mirrors /session-end)."""
        entry = self._active.pop(guild_id, None)
        self._last_channel.pop(guild_id, None)
        if entry is None:
            return
        session_id, recorder, task = entry
        task.cancel()
        try:
            recorder.stop()
        except Exception:
            pass
        try:
            await asyncio.wait_for(self.store.end_session(session_id), timeout=5.0)
        except asyncio.TimeoutError:
            log.warning("end_session timed out for session %s", session_id)
        except Exception:
            log.exception("error ending session %s after failed auto-rejoin", session_id)

    async def _transcription_loop(
        self,
        queue: asyncio.Queue,
        session_id: str,
        player_map: dict[int, str],
    ) -> None:
        while True:
            uid, display, pcm = await queue.get()
            try:
                # Run the (blocking) Whisper call off the event loop so it can
                # never starve the loop, and bail out if it hangs.
                result = await asyncio.wait_for(
                    asyncio.to_thread(self.transcriber.transcribe_pcm, pcm),
                    timeout=10.0,
                )
                if result and result.text:
                    text = result.text.strip()
                    if _is_hallucination(text):
                        log.debug("Dropping hallucination from %s: %r", display, text)
                        continue
                    char = character_name(player_map, uid, display)
                    log.info("[%s / %s] %s", display, char, text)
                    sys.stdout.flush()
                    await self.store.add_line(
                        session_id=session_id,
                        discord_user_id=uid,
                        display_name=char,
                        text=text,
                        confidence=result.confidence,
                    )
            except asyncio.TimeoutError:
                log.warning("transcribe timed out (>10s); skipping chunk from %s", display)
            except Exception:
                log.exception("transcription loop error for chunk from %s", display)
            finally:
                queue.task_done()
                # Yield control so interaction handlers can run promptly.
                await asyncio.sleep(0)


class _SessionListView(discord.ui.View):
    """Buttons for the /session-list embed.

    Shows up to 4 sessions (one row each), with a single Delete All button on
    the last row. Discord views allow only 5 rows (0-4), so the per-session
    rows are capped at 4 to leave room for the Delete All control on row 4.
    """

    def __init__(self, bot: SoulogosBot, sessions: list[dict]) -> None:
        super().__init__(timeout=300)
        for i, session in enumerate(sessions[:4]):
            sid: str = session["id"]
            sname: str = (session.get("name") or sid)[:77]

            btn_del = discord.ui.Button(
                label=f"Delete {sname}",
                style=discord.ButtonStyle.danger,
                custom_id=f"del_{sid}",
                row=i,
            )
            btn_del.callback = _make_delete_callback(bot, sid)

            btn_tx = discord.ui.Button(
                label=f"Transcribe {sname}",
                style=discord.ButtonStyle.secondary,
                custom_id=f"session-transcribe_{sid}",
                row=i,
            )
            btn_tx.callback = _make_transcribe_callback(bot, sid)

            btn_export = discord.ui.Button(
                label=f"Export {sname}",
                style=discord.ButtonStyle.primary,
                custom_id=f"export_{sid}",
                row=i,
            )
            btn_export.callback = _make_export_callback(bot, sid)

            btn_condense = discord.ui.Button(
                label=f"Condense {sname}",
                style=discord.ButtonStyle.success,
                custom_id=f"condense_{sid}",
                row=i,
            )
            btn_condense.callback = _make_condense_callback(bot, sid)

            btn_recap = discord.ui.Button(
                label=f"Recap {sname}",
                style=discord.ButtonStyle.secondary,
                custom_id=f"recap_{sid}",
                row=i,
            )
            btn_recap.callback = _make_recap_callback(bot, sid)

            self.add_item(btn_del)
            self.add_item(btn_tx)
            self.add_item(btn_export)
            self.add_item(btn_condense)
            self.add_item(btn_recap)

        btn_del_all = discord.ui.Button(
            label="Delete All Sessions",
            style=discord.ButtonStyle.danger,
            custom_id="del_all",
            row=4,
        )
        btn_del_all.callback = _make_delete_all_callback(bot)
        self.add_item(btn_del_all)


def _active_recorder(bot: SoulogosBot, guild_id: int) -> Recorder | None:
    """Return the live Recorder for a guild's active session, or None."""
    entry = bot._active.get(guild_id)
    return entry[1] if entry else None


class _RecordingControlView(discord.ui.View):
    """Pause/Resume controls for an active recording session.

    Lives in the channel where /capture-join was called. The two buttons mirror
    the recorder's pause flag: while recording, Pause is enabled and Resume is
    disabled; while paused, the reverse. The bot stays in the voice channel
    either way -- pausing only drops incoming audio.
    """

    def __init__(self, bot: SoulogosBot, guild_id: int, *, paused: bool = False) -> None:
        super().__init__(timeout=None)
        self._bot = bot
        self._guild_id = guild_id

        self.pause_btn = discord.ui.Button(
            label="⏸ Pause Recording",
            style=discord.ButtonStyle.secondary,
            custom_id=f"capture-pause_{guild_id}",
            disabled=paused,
        )
        self.pause_btn.callback = self._on_pause

        self.resume_btn = discord.ui.Button(
            label="▶ Resume Recording",
            style=discord.ButtonStyle.success,
            custom_id=f"capture-resume_{guild_id}",
            disabled=not paused,
        )
        self.resume_btn.callback = self._on_resume

        self.add_item(self.pause_btn)
        self.add_item(self.resume_btn)

    def _sync_buttons(self, paused: bool) -> None:
        self.pause_btn.disabled = paused
        self.resume_btn.disabled = not paused

    async def _toggle(self, interaction: discord.Interaction, paused: bool) -> None:
        recorder = _active_recorder(self._bot, self._guild_id)
        if recorder is None:
            # Session ended out from under the buttons; disable both.
            self.pause_btn.disabled = True
            self.resume_btn.disabled = True
            await interaction.response.edit_message(view=self)
            await interaction.followup.send("No active session.", ephemeral=True)
            return

        if paused:
            recorder.pause()
            message = "⏸ Recording paused."
        else:
            recorder.resume()
            message = "▶ Recording resumed."
        self._sync_buttons(paused)
        await interaction.response.edit_message(view=self)
        await interaction.followup.send(message, ephemeral=True)

    async def _on_pause(self, interaction: discord.Interaction) -> None:
        await self._toggle(interaction, paused=True)

    async def _on_resume(self, interaction: discord.Interaction) -> None:
        await self._toggle(interaction, paused=False)


def _format_status_timestamp(ts: str) -> str:
    """Convert a stored UTC ISO timestamp to local 12-hour time for display.

    e.g. '2026-06-25T20:05:26.402869+00:00' -> '2026-06-25 4:05 PM'.
    No seconds, microseconds, or timezone offset shown. Falls back to the raw
    value if it cannot be parsed.
    """
    try:
        local = datetime.fromisoformat(ts).astimezone()
    except (ValueError, TypeError):
        return ts
    hour = local.strftime("%I").lstrip("0") or "12"
    return f"{local.strftime('%Y-%m-%d')} {hour}:{local.strftime('%M %p')}"


def _build_session_embed(sessions: list[dict]) -> discord.Embed:
    embed = discord.Embed(title="Recording Sessions", color=discord.Color.blurple())
    for s in sessions:
        ended = _format_status_timestamp(s["ended_at"]) if s["ended_at"] else "In progress"
        embed.add_field(
            name=f"{s['name']} (`{s['id']}`)" if s.get("name") else f"Session `{s['id']}`",
            value=f"**Lines:** {s['line_count']} | **Status:** {ended}",
            inline=False,
        )
    if len(sessions) > 4:
        embed.set_footer(
            text=f"Showing buttons for 4 most recent of {len(sessions)} sessions. Use /session-delete for older ones."
        )
    return embed


def _make_delete_callback(bot: SoulogosBot, session_id: str):
    async def callback(interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        deleted = await bot.store.delete_session(session_id, guild_id=interaction.guild.id)
        if not deleted:
            await interaction.response.send_message(
                f"Session `{session_id}` not found.", ephemeral=True
            )
            return

        # Refresh the original list message in place.
        sessions = await bot.store.list_sessions(interaction.guild.id)
        try:
            if not sessions:
                await interaction.message.edit(content="No sessions found.", embed=None, view=None)
            else:
                new_embed = _build_session_embed(sessions)
                new_view = _SessionListView(bot, sessions)
                await interaction.message.edit(embed=new_embed, view=new_view)
        except Exception:
            log.exception("Failed to refresh session list after delete")

        await interaction.response.send_message(
            f"Session `{session_id}` and its transcript deleted.", ephemeral=True
        )
    return callback


def _make_delete_all_callback(bot: SoulogosBot):
    async def callback(interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        await interaction.response.defer(ephemeral=True)
        await bot.store.delete_all_sessions(interaction.guild.id)
        try:
            await interaction.message.edit(content="No sessions found.", embed=None, view=None)
        except Exception:
            log.exception("Failed to refresh session list after delete-all")
        await interaction.followup.send("All sessions deleted.", ephemeral=True)
    return callback


def _format_transcript(lines: list[dict]) -> str:
    return "\n".join(
        f"[{line.get('timestamp', '')}] {line.get('display_name', 'Unknown')}: {line.get('text', '')}"
        for line in lines
    )


def _format_timestamp(ts: str) -> str:
    """Format an ISO datetime string as HH:MM:SS. Degrades gracefully."""
    if not ts:
        return "--:--:--"
    try:
        return datetime.fromisoformat(ts).strftime("%H:%M:%S")
    except ValueError:
        # Best-effort fallback: slice the time portion out of the ISO string.
        return ts[11:19] if len(ts) >= 19 else ts


def _format_transcript_plain(session_id: str, lines: list[dict], name: str = "") -> str:
    header = (
        f"# {name} - Session Transcript: {session_id}"
        if name
        else f"# Session Transcript: {session_id}"
    )
    body = "\n".join(
        f"[{_format_timestamp(line.get('timestamp', ''))}] "
        f"**{line.get('display_name', 'Unknown')}:** {line.get('text', '')}"
        for line in lines
    )
    return f"{header}\n\n{body}\n"


def _format_transcript_timed(lines: list[dict]) -> str:
    """Plain timed transcript for feeding to Claude: '[HH:MM:SS] name: text'."""
    return "\n".join(
        f"[{_format_timestamp(line.get('timestamp', ''))}] "
        f"{line.get('display_name', 'Unknown')}: {line.get('text', '')}"
        for line in lines
    )


def _chunk_by_words(text: str, max_words: int = 3000) -> list[str]:
    """Split text into sections of at most max_words words."""
    words = text.split()
    if not words:
        return [""]
    return [" ".join(words[i:i + max_words]) for i in range(0, len(words), max_words)]


async def _condense(client, prompt: str, content: str, max_tokens: int = 2048) -> str:
    response = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=max_tokens,
        system=prompt,
        messages=[{"role": "user", "content": content}],
    )
    return response.content[0].text


async def _summarize_transcript(client, prompt: str, transcript_text: str, max_tokens: int) -> str:
    """Summarize a transcript with the given prompt.

    For transcripts over 3000 words, condense each ~3000-word section, then make
    one more call to stitch the section summaries into a single unified result.
    """
    if len(transcript_text.split()) > 3000:
        sections = _chunk_by_words(transcript_text, 3000)
        section_summaries = []
        for idx, section in enumerate(sections, 1):
            part = await _condense(
                client,
                prompt,
                f"This is part {idx} of {len(sections)} of a single session "
                f"transcript. Condense just this portion:\n\n{section}",
                max_tokens,
            )
            section_summaries.append(part)
        stitched = "\n\n".join(
            f"--- Section {i} ---\n{s}" for i, s in enumerate(section_summaries, 1)
        )
        return await _condense(
            client,
            prompt,
            "The following are per-section summaries of ONE session, in order. "
            "Stitch them into a single unified result following the same structure "
            "and instructions, merging duplicates and resolving contradictions:\n\n"
            + stitched,
            max_tokens,
        )
    return await _condense(
        client, prompt, f"Session transcript:\n\n{transcript_text}", max_tokens
    )


def _structured_log_path(bot: SoulogosBot, session_id: str):
    """On-disk path for a session's stored structured log."""
    return bot.config.logs_path / f"session_{session_id}_structured.md"


async def _generate_structured_log(bot: SoulogosBot, session_id: str, lines: list[dict]) -> str:
    """Condense the raw transcript into a structured log, store it, return text.

    Runs crown_summary_prompt over the raw timed transcript (preserving the
    >3000-word chunk-and-stitch handling) and OVERWRITES the stored file at
    data/logs/session_{id}_structured.md.
    """
    transcript_text = _format_transcript_timed(_clean_lines_for_llm(lines))
    client = anthropic.AsyncAnthropic(api_key=bot.config.anthropic_api_key)
    structured = await _summarize_transcript(
        client, bot.summary_prompt, transcript_text, max_tokens=2048
    )
    bot.config.logs_path.mkdir(parents=True, exist_ok=True)
    _structured_log_path(bot, session_id).write_text(structured, encoding="utf-8")
    return structured


async def get_or_create_structured_log(bot: SoulogosBot, session_id: str) -> str | None:
    """Return the structured log text for a session.

    If the stored file exists, read and return it. Otherwise generate it from
    the raw transcript, store it, and return it -- WITHOUT posting anywhere
    (silent creation). Returns None when the session has no transcript lines.
    """
    path = _structured_log_path(bot, session_id)
    if path.exists():
        return path.read_text(encoding="utf-8")
    lines = await bot.store.get_lines(session_id)
    if not lines:
        return None
    return await _generate_structured_log(bot, session_id, lines)


def _make_export_callback(bot: SoulogosBot, session_id: str):
    async def callback(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        lines = await bot.store.get_lines(session_id)
        if not lines:
            await interaction.followup.send(
                "No transcript lines found for this session.", ephemeral=True
            )
            return

        session = await bot.store.get_session(session_id)
        name = (session or {}).get("name") or ""

        plain = _format_transcript_plain(session_id, lines, name)

        bot.config.summaries_path.mkdir(parents=True, exist_ok=True)
        out_path = bot.config.summaries_path / f"session_{session_id}_transcript.md"
        out_path.write_text(plain, encoding="utf-8")
        filename = f"session_{session_id}_transcript.md"

        # DM gets the file ephemerally.
        await interaction.followup.send(
            file=discord.File(str(out_path), filename=filename),
            ephemeral=True,
        )

        # DM-only channel gets the same file (a fresh File, since fp is consumed on send).
        channel = bot.get_channel(bot.config.dm_channel_id)
        if channel is not None:
            header = (
                f"📄 **{name}** - Session Transcript" if name else f"📄 **Session {session_id}**"
            )
            await channel.send(header, file=discord.File(str(out_path), filename=filename))

        await interaction.followup.send(
            "Transcript exported and posted to #prep-notes.", ephemeral=True
        )

    return callback


def _make_condense_callback(bot: SoulogosBot, session_id: str):
    async def callback(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        lines = await bot.store.get_lines(session_id)
        if not lines:
            await interaction.followup.send(
                "No transcript lines found for this session.", ephemeral=True
            )
            return

        session = await bot.store.get_session(session_id)
        name = (session or {}).get("name") or ""

        # Condense is the DM's tool to (re)build the structured log: ALWAYS
        # regenerate fresh from the raw transcript and overwrite the stored file.
        try:
            await _generate_structured_log(bot, session_id, lines)
        except Exception as exc:
            log.exception("Anthropic API error condensing session %s", session_id)
            await interaction.followup.send(
                f"Failed to generate debrief: {exc}", ephemeral=True
            )
            return

        out_path = _structured_log_path(bot, session_id)
        filename = f"session_{session_id}_structured.md"

        # DM gets the file ephemerally.
        await interaction.followup.send(
            file=discord.File(str(out_path), filename=filename),
            ephemeral=True,
        )

        # DM-only channel gets the same file (fresh File object).
        channel = bot.get_channel(bot.config.dm_channel_id)
        if channel is not None:
            header = (
                f"🎲 **{name}** - Session Debrief"
                if name
                else f"🎲 **Session {session_id}**"
            )
            await channel.send(header, file=discord.File(str(out_path), filename=filename))

        await interaction.followup.send(
            "Debrief generated and posted to #prep-notes.", ephemeral=True
        )

    return callback


def _make_recap_callback(bot: SoulogosBot, session_id: str):
    async def callback(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        # Recap is built from the STRUCTURED LOG, not the raw transcript. Obtain
        # it (reading the stored file if present, otherwise generating it
        # silently from the transcript).
        try:
            structured = await get_or_create_structured_log(bot, session_id)
        except Exception as exc:
            log.exception("Anthropic API error building structured log for session %s", session_id)
            await interaction.followup.send(
                f"Failed to generate recap: {exc}", ephemeral=True
            )
            return

        if structured is None:
            await interaction.followup.send(
                "No transcript lines found for this session.", ephemeral=True
            )
            return

        session = await bot.store.get_session(session_id)
        name = (session or {}).get("name") or ""

        try:
            client = anthropic.AsyncAnthropic(api_key=bot.config.anthropic_api_key)
            recap = await _condense(
                client, bot.recap_prompt, structured, max_tokens=1024
            )
        except Exception as exc:
            log.exception("Anthropic API error generating recap for session %s", session_id)
            await interaction.followup.send(
                f"Failed to generate recap: {exc}", ephemeral=True
            )
            return

        bot.config.summaries_path.mkdir(parents=True, exist_ok=True)
        out_path = bot.config.summaries_path / f"session_{session_id}_recap.md"
        out_path.write_text(recap, encoding="utf-8")
        filename = f"session_{session_id}_recap.md"

        # DM gets the file ephemerally.
        await interaction.followup.send(
            file=discord.File(str(out_path), filename=filename),
            ephemeral=True,
        )

        # Player-facing channel gets the same file (fresh File object).
        channel = bot.get_channel(bot.config.player_channel_id)
        if channel is not None:
            header = (
                f"📜 **{name}** - Session Recap"
                if name
                else f"📜 **Session {session_id}**"
            )
            await channel.send(header, file=discord.File(str(out_path), filename=filename))

        await interaction.followup.send(
            "Recap generated and posted to #session-log.", ephemeral=True
        )

    return callback


def _make_transcribe_callback(bot: SoulogosBot, session_id: str):
    async def callback(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        lines = await bot.store.get_lines(session_id)
        if not lines:
            await interaction.followup.send(
                f"No transcript lines found for session `{session_id}`.", ephemeral=True
            )
            return

        transcript_text = _format_transcript(lines)

        try:
            client = anthropic.AsyncAnthropic(api_key=bot.config.anthropic_api_key)
            response = await client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=4096,
                system=(
                    "You are summarizing a D&D TTRPG session transcript for a Dungeon Master. "
                    "Produce a structured markdown session summary."
                ),
                messages=[
                    {
                        "role": "user",
                        "content": (
                            "Please summarize this session transcript. Include these sections:\n"
                            "- **Session Overview**\n"
                            "- **Key Events** (in order)\n"
                            "- **NPC Interactions**\n"
                            "- **Player Decisions**\n"
                            "- **Unresolved Threads**\n"
                            "- **DM Notes for Next Session**\n\n"
                            f"Transcript:\n\n{transcript_text}"
                        ),
                    }
                ],
            )
            summary = response.content[0].text
        except Exception as exc:
            log.exception("Anthropic API error for session %s", session_id)
            await interaction.followup.send(
                f"Failed to generate summary: {exc}", ephemeral=True
            )
            return

        bot.config.summaries_path.mkdir(parents=True, exist_ok=True)
        out_path = bot.config.summaries_path / f"session_{session_id}_summary.md"
        out_path.write_text(summary, encoding="utf-8")

        await interaction.followup.send(
            "Summary generated and posted to #prep-notes.",
            file=discord.File(str(out_path), filename=f"session_{session_id}_summary.md"),
            ephemeral=True,
        )
        session = await bot.store.get_session(session_id)
        sname = (session or {}).get("name") or session_id
        channel = bot.get_channel(bot.config.dm_channel_id)
        if channel:
            await channel.send(
                f"📝 **{sname}** - Session Summary",
                file=discord.File(str(out_path), filename=f"session_{session_id}_summary.md"),
            )

    return callback


def _register_commands(bot: SoulogosBot) -> None:
    @bot.tree.error
    async def on_app_command_error(
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        command_name = interaction.command.name if interaction.command else "<unknown>"
        log.exception("Unhandled error in command %s: %s", command_name, error, exc_info=error)
        try:
            if interaction.response.is_done():
                await interaction.followup.send(f"Command error: {error}", ephemeral=True)
            else:
                await interaction.response.send_message(f"Command error: {error}", ephemeral=True)
        except Exception:
            log.exception("Failed to report command error to user")

    @bot.tree.command(name="session-join", description="Join a voice channel and start transcribing")
    @app_commands.describe(
        channel="Voice channel to join (defaults to your current channel)",
        name="Session name (e.g. 'Crown of the Oathbreaker Session 6')",
    )
    async def session_join(
        interaction: discord.Interaction,
        channel: discord.VoiceChannel | None = None,
        name: str = "",
    ) -> None:
        assert interaction.guild is not None
        await interaction.response.defer(ephemeral=True)

        if interaction.guild.id in bot._active:
            await interaction.followup.send("Already recording in this server.", ephemeral=True)
            return

        target = channel or (
            interaction.user.voice.channel  # type: ignore[union-attr]
            if isinstance(interaction.user, discord.Member) and interaction.user.voice
            else None
        )
        if target is None:
            await interaction.followup.send(
                "Join a voice channel first, or pass one as an argument.", ephemeral=True
            )
            return

        try:
            vc = await asyncio.wait_for(
                target.connect(cls=voice_recv.VoiceRecvClient),
                timeout=10.0,
            )
        except Exception:
            # target.connect() can complete the handshake at the Discord level
            # (the gateway dispatches VOICE_STATE_UPDATE and "Voice connection
            # complete" logs) yet never return -- with voice_recv it sometimes
            # awaits a readiness future that never resolves, hanging the join.
            # discord.py attaches the voice client to the guild before the
            # handshake fully settles, so on timeout we adopt that live client
            # instead of stranding the command. Only report failure when no
            # connected client is available.
            recovered = interaction.guild.voice_client
            if (
                isinstance(recovered, voice_recv.VoiceRecvClient)
                and recovered.is_connected()
            ):
                vc = recovered
                log.warning(
                    "target.connect() did not return for %s; recovered live "
                    "voice client from guild",
                    target.name,
                    exc_info=True,
                )
            else:
                log.exception("Failed to connect to voice channel %s", target.name)
                await interaction.followup.send(
                    f"Failed to join **{target.name}** - voice connection timed out.",
                    ephemeral=True,
                )
                return

        player_map = {}
        log.info("Creating session...")
        session_id = await bot.store.create_session(interaction.guild.id, target.id, name)

        queue: asyncio.Queue = asyncio.Queue()
        recorder = Recorder(vc, queue)
        recorder.start()

        task = asyncio.create_task(
            bot._transcription_loop(queue, session_id, player_map)
        )
        bot._active[interaction.guild.id] = (session_id, recorder, task)
        # Remember the channel so an unexpected voice drop can auto-rejoin it,
        # and clear any stale intentional-teardown flag from a prior session.
        bot._last_channel[interaction.guild.id] = target.id
        bot._ending.discard(interaction.guild.id)

        if name:
            confirmation = f"Recording started in **{target.name}** (session `{session_id}`) - {name}"
        else:
            confirmation = f"Recording started in **{target.name}** (session `{session_id}`)."
        # Ephemeral so the Pause/Resume controls are visible only to whoever ran
        # /capture-join -- this command is used in a player-visible channel.
        # The button callbacks update this message via interaction.response.
        # edit_message(), which works for ephemeral messages in the same
        # interaction chain.
        await interaction.followup.send(
            confirmation,
            view=_RecordingControlView(bot, interaction.guild.id),
            ephemeral=True,
        )
        log.info("Session %s started in guild %d / channel %d", session_id, interaction.guild.id, target.id)

    @bot.tree.command(name="session-end", description="Stop transcribing and leave the voice channel")
    async def session_end(interaction: discord.Interaction) -> None:
        log.info("session-end dispatched (guild=%s, user=%s)", interaction.guild_id, interaction.user.id)
        assert interaction.guild is not None
        await interaction.response.defer(ephemeral=True)
        try:
            entry = bot._active.pop(interaction.guild.id, None)
            if entry is None:
                await interaction.followup.send("No active recording in this server.", ephemeral=True)
                return

            # Flag this as an intentional teardown so the disconnect below does
            # not trip the unexpected-disconnect auto-rejoin handler.
            bot._ending.add(interaction.guild.id)
            bot._last_channel.pop(interaction.guild.id, None)

            session_id, recorder, task = entry
            task.cancel()
            recorder.stop()

            if interaction.guild.voice_client:
                asyncio.ensure_future(interaction.guild.voice_client.disconnect())

            try:
                await asyncio.wait_for(bot.store.end_session(session_id), timeout=5.0)
            except asyncio.TimeoutError:
                log.warning("end_session timed out for session %s", session_id)

            await interaction.followup.send(f"Recording ended (session `{session_id}`).")
            log.info("Session %s ended in guild %d", session_id, interaction.guild.id)
        except Exception as exc:
            log.exception("session_end error: %s", exc)
            await interaction.followup.send(f"Error ending session: {exc}", ephemeral=True)

    @bot.tree.command(name="session-pause", description="Pause the active recording (stay in the voice channel)")
    async def session_pause(interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        recorder = _active_recorder(bot, interaction.guild.id)
        if recorder is None:
            await interaction.response.send_message("No active session.", ephemeral=True)
            return
        recorder.pause()
        await interaction.response.send_message("⏸ Recording paused.", ephemeral=True)

    @bot.tree.command(name="session-resume", description="Resume the active recording")
    async def session_resume(interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        recorder = _active_recorder(bot, interaction.guild.id)
        if recorder is None:
            await interaction.response.send_message("No active session.", ephemeral=True)
            return
        recorder.resume()
        await interaction.response.send_message("▶ Recording resumed.", ephemeral=True)

    @bot.tree.command(name="session-list", description="List all recording sessions for this server")
    async def session_list(interaction: discord.Interaction) -> None:
        assert interaction.guild is not None

        sessions = await bot.store.list_sessions(interaction.guild.id)
        if not sessions:
            await interaction.response.send_message("No sessions found for this server.", ephemeral=True)
            return

        embed = _build_session_embed(sessions)
        view = _SessionListView(bot, sessions)
        await interaction.response.send_message(embed=embed, view=view)

    @bot.tree.command(name="session-delete", description="Delete a session and all its transcript lines")
    @app_commands.describe(session_id="Session ID to delete (e.g. 20260624_131025)")
    async def session_delete(
        interaction: discord.Interaction,
        session_id: str,
    ) -> None:
        assert interaction.guild is not None

        deleted = await bot.store.delete_session(session_id, guild_id=interaction.guild.id)
        if deleted:
            await interaction.response.send_message(
                f"Session `{session_id}` and its transcript deleted.", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"Session `{session_id}` not found in this server.", ephemeral=True
            )

    @bot.tree.command(name="session-merge", description="Merge source sessions into a target session")
    @app_commands.describe(
        target_id="Session ID to merge into",
        source_ids="Comma-separated session IDs to merge from (deleted after merge)",
    )
    async def session_merge(
        interaction: discord.Interaction,
        target_id: str,
        source_ids: str,
    ) -> None:
        assert interaction.guild is not None
        if interaction.channel_id != bot.config.dm_channel_id:
            await interaction.response.send_message(
                "This command can only be used in the DM channel.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)

        parsed = [s.strip() for s in source_ids.split(",") if s.strip()]
        if not parsed:
            await interaction.followup.send("No source session IDs provided.", ephemeral=True)
            return

        try:
            result = await bot.store.merge_sessions(target_id, parsed, interaction.guild.id)
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        except Exception as exc:
            log.exception("Error merging sessions into %s", target_id)
            await interaction.followup.send(f"Failed to merge sessions: {exc}", ephemeral=True)
            return

        msg = f"Merged {result['merged_count']} lines into session `{target_id}`."
        if result["skipped"]:
            skipped_list = ", ".join(f"`{s}`" for s in result["skipped"])
            msg += f"\nSkipped (not found in this server): {skipped_list}"
        await interaction.followup.send(msg, ephemeral=True)

    @bot.tree.command(name="session-condense", description="Generate or regenerate the structured debrief for a session")
    @app_commands.describe(session_id="Session ID to condense")
    async def session_condense(
        interaction: discord.Interaction,
        session_id: str,
    ) -> None:
        assert interaction.guild is not None
        if interaction.channel_id != bot.config.dm_channel_id:
            await interaction.response.send_message(
                "This command can only be used in the DM channel.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)

        lines = await bot.store.get_lines(session_id)
        if not lines:
            await interaction.followup.send(
                f"No transcript lines found for session `{session_id}`.", ephemeral=True
            )
            return

        session = await bot.store.get_session(session_id)
        name = (session or {}).get("name") or ""

        try:
            await _generate_structured_log(bot, session_id, lines)
        except Exception as exc:
            log.exception("Anthropic API error condensing session %s", session_id)
            await interaction.followup.send(f"Failed to generate debrief: {exc}", ephemeral=True)
            return

        out_path = _structured_log_path(bot, session_id)
        filename = f"session_{session_id}_structured.md"

        await interaction.followup.send(
            file=discord.File(str(out_path), filename=filename),
            ephemeral=True,
        )

        channel = bot.get_channel(bot.config.dm_channel_id)
        if channel is not None:
            header = (
                f"🎲 **{name}** - Session Debrief"
                if name
                else f"🎲 **Session {session_id}**"
            )
            await channel.send(header, file=discord.File(str(out_path), filename=filename))

        await interaction.followup.send(
            "Debrief generated and posted to #prep-notes.", ephemeral=True
        )

    @bot.tree.command(name="session-recap", description="Generate a player-facing recap for a session")
    @app_commands.describe(session_id="Session ID to recap")
    async def session_recap(
        interaction: discord.Interaction,
        session_id: str,
    ) -> None:
        assert interaction.guild is not None
        if interaction.channel_id != bot.config.dm_channel_id:
            await interaction.response.send_message(
                "This command can only be used in the DM channel.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)

        try:
            structured = await get_or_create_structured_log(bot, session_id)
        except Exception as exc:
            log.exception("Anthropic API error building structured log for session %s", session_id)
            await interaction.followup.send(f"Failed to generate recap: {exc}", ephemeral=True)
            return

        if structured is None:
            await interaction.followup.send(
                f"No transcript lines found for session `{session_id}`.", ephemeral=True
            )
            return

        session = await bot.store.get_session(session_id)
        name = (session or {}).get("name") or ""

        try:
            client = anthropic.AsyncAnthropic(api_key=bot.config.anthropic_api_key)
            recap = await _condense(client, bot.recap_prompt, structured, max_tokens=1024)
        except Exception as exc:
            log.exception("Anthropic API error generating recap for session %s", session_id)
            await interaction.followup.send(f"Failed to generate recap: {exc}", ephemeral=True)
            return

        bot.config.summaries_path.mkdir(parents=True, exist_ok=True)
        out_path = bot.config.summaries_path / f"session_{session_id}_recap.md"
        out_path.write_text(recap, encoding="utf-8")
        filename = f"session_{session_id}_recap.md"

        await interaction.followup.send(
            file=discord.File(str(out_path), filename=filename),
            ephemeral=True,
        )

        channel = bot.get_channel(bot.config.player_channel_id)
        if channel is not None:
            header = (
                f"📜 **{name}** - Session Recap"
                if name
                else f"📜 **Session {session_id}**"
            )
            await channel.send(header, file=discord.File(str(out_path), filename=filename))

        await interaction.followup.send(
            "Recap generated and posted to #session-log.", ephemeral=True
        )
