import logging
import sys

from .bot import SoulogosBot
from .config import load_config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    stream=sys.stdout,
)

# Cosmetic log cleanup: these voice_recv loggers spam at INFO during recording
# ("Received unexpected rtcp packet", "WS payload has extra keys"). Raise them to
# WARNING so the noise is suppressed while real warnings still surface.
logging.getLogger("discord.ext.voice_recv.reader").setLevel(logging.WARNING)
logging.getLogger("discord.ext.voice_recv.gateway").setLevel(logging.WARNING)


def main() -> None:
    config = load_config()
    bot = SoulogosBot(config)
    bot.run(config.discord_bot_token)


if __name__ == "__main__":
    main()
