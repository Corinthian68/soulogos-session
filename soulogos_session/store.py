import aiosqlite
from datetime import datetime, timezone
from pathlib import Path


_CREATE_SESSIONS = """
CREATE TABLE IF NOT EXISTS sessions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    channel_id  INTEGER NOT NULL,
    started_at  TEXT    NOT NULL,
    ended_at    TEXT
)
"""

_CREATE_TRANSCRIPT_LINES = """
CREATE TABLE IF NOT EXISTS transcript_lines (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      INTEGER NOT NULL REFERENCES sessions(id),
    timestamp       TEXT    NOT NULL,
    discord_user_id INTEGER NOT NULL,
    display_name    TEXT    NOT NULL,
    text            TEXT    NOT NULL,
    confidence      REAL
)
"""

_CREATE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_lines_session ON transcript_lines(session_id)
"""


class SessionStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path

    async def init(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(_CREATE_SESSIONS)
            await db.execute(_CREATE_TRANSCRIPT_LINES)
            await db.execute(_CREATE_INDEX)
            await db.commit()

    async def create_session(self, guild_id: int, channel_id: int) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "INSERT INTO sessions (guild_id, channel_id, started_at) VALUES (?, ?, ?)",
                (guild_id, channel_id, _now()),
            )
            await db.commit()
            return cursor.lastrowid  # type: ignore[return-value]

    async def end_session(self, session_id: int) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE sessions SET ended_at = ? WHERE id = ?",
                (_now(), session_id),
            )
            await db.commit()

    async def add_line(
        self,
        *,
        session_id: int,
        discord_user_id: int,
        display_name: str,
        text: str,
        confidence: float,
    ) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO transcript_lines
                    (session_id, timestamp, discord_user_id, display_name, text, confidence)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (session_id, _now(), discord_user_id, display_name, text, confidence),
            )
            await db.commit()

    async def get_lines(self, session_id: int) -> list[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM transcript_lines WHERE session_id = ? ORDER BY timestamp",
                (session_id,),
            )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
