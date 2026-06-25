import asyncio
import logging
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

        # Active sessions: guild_id -> (session_id, Recorder, asyncio.Task)
        self._active: dict[int, tuple[str, Recorder, asyncio.Task]] = {}

        _register_commands(self)

    async def setup_hook(self) -> None:
        await self.store.init()
        guild = discord.Object(id=1433893663322149067)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)
        log.info("Commands synced.")

    async def on_ready(self) -> None:
        log.info("Logged in as %s (id=%s)", self.user, self.user.id)  # type: ignore[union-attr]

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
                    char = character_name(player_map, uid, display)
                    log.info("[%s / %s] %s", display, char, result.text)
                    await self.store.add_line(
                        session_id=session_id,
                        discord_user_id=uid,
                        display_name=char,
                        text=result.text,
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
    """Buttons for the /capture-list embed.

    Shows up to 4 sessions (one row each), with a single Delete All button on
    the last row. Discord views allow only 5 rows (0-4), so the per-session
    rows are capped at 4 to leave room for the Delete All control on row 4.
    """

    def __init__(self, bot: SoulogosBot, sessions: list[dict]) -> None:
        super().__init__(timeout=300)
        for i, session in enumerate(sessions[:4]):
            sid: str = session["id"]
            sname: str = session.get("name") or sid

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
                custom_id=f"capture-transcribe_{sid}",
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

            self.add_item(btn_del)
            self.add_item(btn_tx)
            self.add_item(btn_export)

        btn_del_all = discord.ui.Button(
            label="Delete All Sessions",
            style=discord.ButtonStyle.danger,
            custom_id="del_all",
            row=4,
        )
        btn_del_all.callback = _make_delete_all_callback(bot)
        self.add_item(btn_del_all)


# Channel where exported transcripts are posted (the #session-log channel).
_SESSION_LOG_CHANNEL_ID = 1499170547601506355


def _build_session_embed(sessions: list[dict]) -> discord.Embed:
    embed = discord.Embed(title="Recording Sessions", color=discord.Color.blurple())
    for s in sessions:
        ended = s["ended_at"] or "In progress"
        embed.add_field(
            name=f"{s['name']} (`{s['id']}`)" if s.get("name") else f"Session `{s['id']}`",
            value=f"**Lines:** {s['line_count']} | **Status:** {ended}",
            inline=False,
        )
    if len(sessions) > 4:
        embed.set_footer(
            text=f"Showing buttons for 4 most recent of {len(sessions)} sessions. Use /capture-delete for older ones."
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


def _format_transcript_fancy(session_id: str, lines: list[dict], name: str = "") -> str:
    header = (
        f"## 🎲 {name} - Session Transcript: {session_id}"
        if name
        else f"## 🎲 Session Transcript: {session_id}"
    )
    body = "\n".join(
        f"🗣️ `[{_format_timestamp(line.get('timestamp', ''))}]` "
        f"**{line.get('display_name', 'Unknown')}:** {line.get('text', '')}"
        for line in lines
    )
    return (
        f"{header}\n"
        f"---\n"
        f"{body}\n"
        f"---\n"
        f"*Transcribed by Soulogos Session*"
    )


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
        fancy = _format_transcript_fancy(session_id, lines, name)

        bot.config.summaries_path.mkdir(parents=True, exist_ok=True)
        out_path = bot.config.summaries_path / f"session_{session_id}_transcript.md"
        out_path.write_text(plain, encoding="utf-8")

        await interaction.followup.send(
            file=discord.File(str(out_path), filename=f"session_{session_id}_transcript.md"),
            ephemeral=True,
        )

        channel = bot.get_channel(_SESSION_LOG_CHANNEL_ID)
        if channel is not None:
            await channel.send(fancy)

        await interaction.followup.send(
            "Transcript exported and posted to #session-log.", ephemeral=True
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
            f"Summary for session `{session_id}`:",
            file=discord.File(str(out_path), filename=f"session_{session_id}_summary.md"),
            ephemeral=True,
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

    @bot.tree.command(name="capture-join", description="Join a voice channel and start transcribing")
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

        vc = await target.connect(cls=voice_recv.VoiceRecvClient)
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

        if name:
            confirmation = f"Recording started in **{target.name}** (session `{session_id}`) - {name}"
        else:
            confirmation = f"Recording started in **{target.name}** (session `{session_id}`)."
        await interaction.followup.send(confirmation)
        log.info("Session %s started in guild %d / channel %d", session_id, interaction.guild.id, target.id)

    @bot.tree.command(name="capture-end", description="Stop transcribing and leave the voice channel")
    async def session_end(interaction: discord.Interaction) -> None:
        log.info("capture-end dispatched (guild=%s, user=%s)", interaction.guild_id, interaction.user.id)
        assert interaction.guild is not None
        await interaction.response.defer(ephemeral=True)
        try:
            entry = bot._active.pop(interaction.guild.id, None)
            if entry is None:
                await interaction.followup.send("No active recording in this server.", ephemeral=True)
                return

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

    @bot.tree.command(name="capture-list", description="List all recording sessions for this server")
    async def session_list(interaction: discord.Interaction) -> None:
        assert interaction.guild is not None

        sessions = await bot.store.list_sessions(interaction.guild.id)
        if not sessions:
            await interaction.response.send_message("No sessions found for this server.", ephemeral=True)
            return

        embed = _build_session_embed(sessions)
        view = _SessionListView(bot, sessions)
        await interaction.response.send_message(embed=embed, view=view)

    @bot.tree.command(name="capture-delete", description="Delete a session and all its transcript lines")
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
