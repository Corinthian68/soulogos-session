from dataclasses import dataclass
from pathlib import Path
import os

from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    discord_bot_token: str
    soulogos_db_path: Path
    session_db_path: Path
    whisper_model: str
    whisper_device: str
    anthropic_api_key: str = ""
    assemblyai_api_key: str = ""
    summaries_path: Path = Path("data/summaries")
    logs_path: Path = Path("data/logs")
    summary_prompt_path: Path = Path("data/prompts/crown_summary_prompt.txt")
    recap_prompt_path: Path = Path("data/prompts/crown_recap_prompt.txt")
    stt_config_path: Path = Path("data/stt_config.json")
    # prep-notes channel (DM only)
    dm_channel_id: int = 1499171448043081911
    # session-log channel (players can see it)
    player_channel_id: int = 1499170547601506355


def load_config() -> Config:
    load_dotenv()

    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_BOT_TOKEN is not set")

    return Config(
        discord_bot_token=token,
        soulogos_db_path=Path(os.environ.get("SOULOGOS_DB_PATH", "data/memory.sqlite")),
        session_db_path=Path(os.environ.get("SESSION_DB_PATH", "data/session.sqlite")),
        whisper_model=os.environ.get("WHISPER_MODEL", "base"),
        whisper_device=os.environ.get("WHISPER_DEVICE", "cpu"),
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
        assemblyai_api_key=os.environ.get("ASSEMBLYAI_API_KEY", ""),
        summaries_path=Path(os.environ.get("SUMMARIES_PATH", "data/summaries")),
        logs_path=Path(os.environ.get("LOGS_PATH", "data/logs")),
        summary_prompt_path=Path(
            os.environ.get("SUMMARY_PROMPT_PATH", "data/prompts/crown_summary_prompt.txt")
        ),
        recap_prompt_path=Path(
            os.environ.get("RECAP_PROMPT_PATH", "data/prompts/crown_recap_prompt.txt")
        ),
        dm_channel_id=int(os.environ.get("DM_CHANNEL_ID", "1499171448043081911")),
        player_channel_id=int(os.environ.get("PLAYER_CHANNEL_ID", "1499170547601506355")),
    )
