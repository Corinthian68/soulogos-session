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
    summaries_path: Path = Path("data/summaries")


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
        summaries_path=Path(os.environ.get("SUMMARIES_PATH", "data/summaries")),
    )
