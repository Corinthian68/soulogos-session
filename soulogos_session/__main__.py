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


def main() -> None:
    config = load_config()
    bot = SoulogosBot(config)
    bot.run(config.discord_bot_token)


if __name__ == "__main__":
    main()
