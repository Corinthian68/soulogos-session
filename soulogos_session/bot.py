import asyncio
import logging

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
        self._active: dict[int, tuple[int, Recorder, asyncio.Task]] = {}

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
        session_id: int,
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


def _register_commands(bot: SoulogosBot) -> None:
    @bot.tree.command(name="session-join", description="Join a voice channel and start transcribing")
    @app_commands.describe(channel="Voice channel to join (defaults to your current channel)")
    async def session_join(
        interaction: discord.Interaction,
        channel: discord.VoiceChannel | None = None,
    ) -> None:
        assert interaction.guild is not None

        if interaction.guild.id in bot._active:
            await interaction.response.send_message("Already recording in this server.", ephemeral=True)
            return

        target = channel or (
            interaction.user.voice.channel  # type: ignore[union-attr]
            if isinstance(interaction.user, discord.Member) and interaction.user.voice
            else None
        )
        if target is None:
            await interaction.response.send_message(
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

        await interaction.response.send_message(
            f"Recording started in **{target.name}** (session {session_id})."
        )
        log.info("Session %d started in guild %d / channel %d", session_id, interaction.guild.id, target.id)

    @bot.tree.command(name="session-end", description="Stop transcribing and leave the voice channel")
    async def session_end(interaction: discord.Interaction) -> None:
        assert interaction.guild is not None

        entry = bot._active.pop(interaction.guild.id, None)
        if entry is None:
            await interaction.response.send_message("No active recording in this server.", ephemeral=True)
            return

        session_id, recorder, task = entry
        recorder.stop()
        task.cancel()

        if interaction.guild.voice_client:
            await interaction.guild.voice_client.disconnect()

        await bot.store.end_session(session_id)
        await interaction.response.send_message(f"Recording ended (session {session_id}).")
        log.info("Session %d ended in guild %d", session_id, interaction.guild.id)
