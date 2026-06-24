import asyncio
import logging

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
        await self.tree.sync()
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
            result = self.transcriber.transcribe_pcm(pcm)
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
            queue.task_done()


class _SessionListView(discord.ui.View):
    """Buttons for the /session-list embed. Shows up to 5 sessions (one row each)."""

    def __init__(self, bot: SoulogosBot, sessions: list[dict]) -> None:
        super().__init__(timeout=300)
        for i, session in enumerate(sessions[:5]):
            sid: str = session["id"]

            btn_del = discord.ui.Button(
                label=f"Delete {sid}",
                style=discord.ButtonStyle.danger,
                custom_id=f"del_{sid}",
                row=i,
            )
            btn_del.callback = _make_delete_callback(bot, sid)

            btn_tx = discord.ui.Button(
                label=f"Transcribe {sid}",
                style=discord.ButtonStyle.secondary,
                custom_id=f"tx_{sid}",
                row=i,
            )
            btn_tx.callback = _make_transcribe_callback(bot, sid)

            self.add_item(btn_del)
            self.add_item(btn_tx)


def _make_delete_callback(bot: SoulogosBot, session_id: str):
    async def callback(interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        deleted = await bot.store.delete_session(session_id, guild_id=interaction.guild.id)
        if deleted:
            await interaction.response.send_message(
                f"Session `{session_id}` and its transcript deleted.", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"Session `{session_id}` not found.", ephemeral=True
            )
    return callback


def _format_transcript(lines: list[dict]) -> str:
    return "\n".join(
        f"[{line.get('timestamp', '')}] {line.get('display_name', 'Unknown')}: {line.get('text', '')}"
        for line in lines
    )


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
    @bot.tree.command(name="session-join", description="Join a voice channel and start transcribing")
    @app_commands.describe(channel="Voice channel to join (defaults to your current channel)")
    async def session_join(
        interaction: discord.Interaction,
        channel: discord.VoiceChannel | None = None,
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
        player_map = await load_player_map(bot.config.soulogos_db_path)
        session_id = await bot.store.create_session(interaction.guild.id, target.id)

        queue: asyncio.Queue = asyncio.Queue()
        recorder = Recorder(vc, queue)
        recorder.start()

        task = asyncio.create_task(
            bot._transcription_loop(queue, session_id, player_map)
        )
        bot._active[interaction.guild.id] = (session_id, recorder, task)

        await interaction.followup.send(
            f"Recording started in **{target.name}** (session `{session_id}`)."
        )
        log.info("Session %s started in guild %d / channel %d", session_id, interaction.guild.id, target.id)

    @bot.tree.command(name="session-end", description="Stop transcribing and leave the voice channel")
    async def session_end(interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        await interaction.response.defer(ephemeral=True)

        entry = bot._active.pop(interaction.guild.id, None)
        if entry is None:
            await interaction.followup.send("No active recording in this server.", ephemeral=True)
            return

        session_id, recorder, task = entry
        recorder.stop()
        task.cancel()

        if interaction.guild.voice_client:
            await interaction.guild.voice_client.disconnect()

        await bot.store.end_session(session_id)
        await interaction.followup.send(f"Recording ended (session `{session_id}`).")
        log.info("Session %s ended in guild %d", session_id, interaction.guild.id)

    @bot.tree.command(name="session-list", description="List all recording sessions for this server")
    async def session_list(interaction: discord.Interaction) -> None:
        assert interaction.guild is not None

        sessions = await bot.store.list_sessions(interaction.guild.id)
        if not sessions:
            await interaction.response.send_message("No sessions found for this server.", ephemeral=True)
            return

        embed = discord.Embed(title="Recording Sessions", color=discord.Color.blurple())
        for s in sessions:
            ended = s["ended_at"] or "In progress"
            embed.add_field(
                name=f"Session `{s['id']}`",
                value=(
                    f"**Started:** {s['started_at']}\n"
                    f"**Ended:** {ended}\n"
                    f"**Lines:** {s['line_count']}"
                ),
                inline=False,
            )

        view = _SessionListView(bot, sessions)
        if len(sessions) > 5:
            embed.set_footer(text=f"Showing buttons for 5 most recent of {len(sessions)} sessions. Use /session-delete for older ones.")

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
