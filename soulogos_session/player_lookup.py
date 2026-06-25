"""
Read player records from the Soulogos main memory.sqlite.

Expected schema (read-only, never written here):
    players(discord_user_id INTEGER, character_name TEXT, ...)

Falls back to an empty map if the DB doesn't exist or the table is absent,
so the bot degrades gracefully when running without Soulogos installed.
"""

import aiosqlite
from pathlib import Path


async def load_player_map(db_path: Path) -> dict[int, str]:
    """Return {discord_user_id: character_name} for all known players."""
    if not db_path.exists():
        return {}

    try:
        async with aiosqlite.connect(db_path) as db:
            cursor = await db.execute(
                "SELECT discord_user_id, character_name FROM players"
            )
            rows = await cursor.fetchall()
            return {int(row[0]): row[1] for row in rows if row[0] is not None}
    except Exception:
        # Missing table, wrong schema, locked file -- silently degrade.
        return {}


def character_name(player_map: dict[int, str], discord_user_id: int, fallback: str) -> str:
    return player_map.get(discord_user_id, fallback)
