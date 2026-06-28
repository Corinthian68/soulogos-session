import asyncio
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


_CREATE_SESSIONS = """
CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT    PRIMARY KEY,
    guild_id    INTEGER NOT NULL,
    channel_id  INTEGER NOT NULL,
    started_at  TEXT    NOT NULL,
    ended_at    TEXT,
    name        TEXT
)
"""

_CREATE_TRANSCRIPT_LINES = """
CREATE TABLE IF NOT EXISTS transcript_lines (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT    NOT NULL REFERENCES sessions(id),
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


def _sync_init(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(db_path)
    try:
        db.execute(_CREATE_SESSIONS)
        db.execute(_CREATE_TRANSCRIPT_LINES)
        db.execute(_CREATE_INDEX)
        # Migrate older databases that predate the `name` column.
        existing = {row[1] for row in db.execute("PRAGMA table_info(sessions)")}
        if "name" not in existing:
            db.execute("ALTER TABLE sessions ADD COLUMN name TEXT")
        db.commit()
    finally:
        db.close()


def _sync_create_session(
    db_path: Path, guild_id: int, channel_id: int, name: str = ""
) -> str:
    base_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    db = sqlite3.connect(db_path)
    try:
        session_id = base_id
        suffix = 1
        while True:
            try:
                db.execute(
                    "INSERT INTO sessions (id, guild_id, channel_id, started_at, name) VALUES (?, ?, ?, ?, ?)",
                    (session_id, guild_id, channel_id, _now(), name),
                )
                db.commit()
                return session_id
            except sqlite3.IntegrityError:
                suffix += 1
                session_id = f"{base_id}_{suffix}"
    finally:
        db.close()


def _sync_end_session(db_path: Path, session_id: str) -> None:
    db = sqlite3.connect(db_path)
    try:
        db.execute(
            "UPDATE sessions SET ended_at = ? WHERE id = ?",
            (_now(), session_id),
        )
        db.commit()
    finally:
        db.close()


def _sync_add_line(
    db_path: Path,
    session_id: str,
    discord_user_id: int,
    display_name: str,
    text: str,
    confidence: float,
) -> None:
    db = sqlite3.connect(db_path)
    try:
        db.execute(
            """
            INSERT INTO transcript_lines
                (session_id, timestamp, discord_user_id, display_name, text, confidence)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (session_id, _now(), discord_user_id, display_name, text, confidence),
        )
        db.commit()
    finally:
        db.close()


def _sync_get_lines(db_path: Path, session_id: str) -> list[dict]:
    db = sqlite3.connect(db_path)
    try:
        db.row_factory = sqlite3.Row
        cursor = db.execute(
            "SELECT * FROM transcript_lines WHERE session_id = ? ORDER BY timestamp",
            (session_id,),
        )
        rows = cursor.fetchall()
        return [dict(r) for r in rows]
    finally:
        db.close()


def _sync_get_session(db_path: Path, session_id: str) -> dict | None:
    db = sqlite3.connect(db_path)
    try:
        db.row_factory = sqlite3.Row
        cursor = db.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        )
        row = cursor.fetchone()
        return dict(row) if row is not None else None
    finally:
        db.close()


def _sync_list_sessions(db_path: Path, guild_id: int) -> list[dict]:
    db = sqlite3.connect(db_path)
    try:
        db.row_factory = sqlite3.Row
        cursor = db.execute(
            """
            SELECT s.id, s.guild_id, s.channel_id, s.started_at, s.ended_at, s.name,
                   COUNT(l.id) AS line_count
            FROM sessions s
            LEFT JOIN transcript_lines l ON l.session_id = s.id
            WHERE s.guild_id = ?
            GROUP BY s.id
            ORDER BY s.started_at DESC
            """,
            (guild_id,),
        )
        rows = cursor.fetchall()
        return [dict(r) for r in rows]
    finally:
        db.close()


def _sync_delete_session(
    db_path: Path, session_id: str, guild_id: int | None
) -> bool:
    db = sqlite3.connect(db_path)
    try:
        if guild_id is not None:
            cursor = db.execute(
                "SELECT id FROM sessions WHERE id = ? AND guild_id = ?",
                (session_id, guild_id),
            )
            if cursor.fetchone() is None:
                return False
        db.execute(
            "DELETE FROM transcript_lines WHERE session_id = ?", (session_id,)
        )
        cursor = db.execute(
            "DELETE FROM sessions WHERE id = ?", (session_id,)
        )
        db.commit()
        return cursor.rowcount > 0
    finally:
        db.close()


def _sync_merge_sessions(db_path: Path, target_id: str, source_ids: list[str], guild_id: int) -> dict:
    """Move all transcript lines from source sessions into target, delete sources.
    Returns dict with keys: merged_count (int), skipped (list of invalid ids)."""
    db = sqlite3.connect(db_path)
    try:
        row = db.execute(
            "SELECT id FROM sessions WHERE id = ? AND guild_id = ?",
            (target_id, guild_id),
        ).fetchone()
        if row is None:
            raise ValueError(f"Target session {target_id!r} not found in this server.")

        skipped = []
        merged_count = 0

        for source_id in source_ids:
            if db.execute(
                "SELECT id FROM sessions WHERE id = ? AND guild_id = ?",
                (source_id, guild_id),
            ).fetchone() is None:
                skipped.append(source_id)
                continue
            cursor = db.execute(
                "UPDATE transcript_lines SET session_id = ? WHERE session_id = ?",
                (target_id, source_id),
            )
            merged_count += cursor.rowcount
            db.execute("DELETE FROM sessions WHERE id = ?", (source_id,))

        db.commit()
        return {"merged_count": merged_count, "skipped": skipped}
    finally:
        db.close()


def _sync_delete_all_sessions(db_path: Path, guild_id: int) -> int:
    db = sqlite3.connect(db_path)
    try:
        db.execute(
            """
            DELETE FROM transcript_lines
            WHERE session_id IN (SELECT id FROM sessions WHERE guild_id = ?)
            """,
            (guild_id,),
        )
        cursor = db.execute(
            "DELETE FROM sessions WHERE guild_id = ?", (guild_id,)
        )
        db.commit()
        return cursor.rowcount
    finally:
        db.close()


class SessionStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path

    async def init(self) -> None:
        await asyncio.to_thread(_sync_init, self.db_path)

    async def create_session(self, guild_id: int, channel_id: int, name: str = "") -> str:
        return await asyncio.to_thread(
            _sync_create_session, self.db_path, guild_id, channel_id, name
        )

    async def get_session(self, session_id: str) -> dict | None:
        return await asyncio.to_thread(_sync_get_session, self.db_path, session_id)

    async def end_session(self, session_id: str) -> None:
        await asyncio.to_thread(_sync_end_session, self.db_path, session_id)

    async def add_line(
        self,
        *,
        session_id: str,
        discord_user_id: int,
        display_name: str,
        text: str,
        confidence: float,
    ) -> None:
        await asyncio.to_thread(
            _sync_add_line,
            self.db_path,
            session_id,
            discord_user_id,
            display_name,
            text,
            confidence,
        )

    async def get_lines(self, session_id: str) -> list[dict]:
        return await asyncio.to_thread(_sync_get_lines, self.db_path, session_id)

    async def list_sessions(self, guild_id: int) -> list[dict]:
        return await asyncio.to_thread(_sync_list_sessions, self.db_path, guild_id)

    async def delete_session(self, session_id: str, guild_id: int | None = None) -> bool:
        return await asyncio.to_thread(
            _sync_delete_session, self.db_path, session_id, guild_id
        )

    async def delete_all_sessions(self, guild_id: int) -> int:
        return await asyncio.to_thread(
            _sync_delete_all_sessions, self.db_path, guild_id
        )

    async def merge_sessions(self, target_id: str, source_ids: list[str], guild_id: int) -> dict:
        return await asyncio.to_thread(
            _sync_merge_sessions, self.db_path, target_id, source_ids, guild_id
        )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
